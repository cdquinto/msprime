#
# Copyright (C) 2014 Jerome Kelleher <jerome.kelleher@well.ox.ac.uk>
#
# This file is part of msprime.
#
# msprime is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# msprime is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with msprime.  If not, see <http://www.gnu.org/licenses/>.
#
"""
Module responsible to generating and reading tree files.
"""
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import collections
import heapq
import json
import os
import random
import tempfile

import h5py
import numpy as np

import _msprime
from _msprime import InputError
from _msprime import LibraryError


def harmonic_number(n):
    """
    Returns the nth Harmonic number.
    """
    return sum(1 / k for k in range(1, n + 1))

def simulate_trees(sample_size, num_loci, scaled_recombination_rate,
        population_models=[], random_seed=None, max_memory="10M"):
    """
    Simulates the coalescent with recombination under the specified model
    parameters and returns an iterator over the resulting trees.
    """
    sim = TreeSimulator(sample_size)
    sim.set_num_loci(num_loci)
    sim.set_scaled_recombination_rate(scaled_recombination_rate)
    if random_seed is not None:
        sim.set_random_seed(random_seed)
    sim.set_max_memory(max_memory)
    for m in population_models:
        sim.add_population_model(m)
    tree_sequence = sim.run()
    return tree_sequence.sparse_trees()

def simulate_tree(sample_size, population_models=[], random_seed=None,
        max_memory="10M"):
    """
    Simulates the coalescent at a single locus for the specified sample size
    under the specified list of population models.
    """
    iterator = simulate_trees(sample_size, 1, 0, population_models,
            random_seed, max_memory)
    l, pi, tau = next(iterator)
    return pi, tau

class TreeSimulator(object):
    """
    Class to simulate trees under the standard neutral coalescent with
    recombination.
    """
    def __init__(self, sample_size):
        self._sample_size = sample_size
        self._scaled_recombination_rate = 1.0
        self._num_loci = 1
        self._population_models = []
        self._random_seed = None
        self._segment_block_size = None
        self._avl_node_block_size = None
        self._node_mapping_block_size = None
        self._coalescence_record_block_size = None
        self._max_memory = None
        self._ll_sim = None

    def get_sample_size(self):
        return self._sample_size

    def get_scaled_recombination_rate(self):
        return self._scaled_recombination_rate

    def get_num_loci(self):
        return self._num_loci

    def get_num_breakpoints(self):
        return self._ll_sim.get_num_breakpoints()

    def get_used_memory(self):
        return self._ll_sim.get_used_memory()

    def get_time(self):
        return self._ll_sim.get_time()

    def get_num_avl_node_blocks(self):
        return self._ll_sim.get_num_avl_node_blocks()

    def get_num_coalescence_record_blocks(self):
        return self._ll_sim.get_num_coalescence_record_blocks()

    def get_num_node_mapping_blocks(self):
        return self._ll_sim.get_num_node_mapping_blocks()

    def get_num_segment_blocks(self):
        return self._ll_sim.get_num_segment_blocks()

    def get_num_coancestry_events(self):
        return self._ll_sim.get_num_coancestry_events()

    def get_num_recombination_events(self):
        return self._ll_sim.get_num_recombination_events()

    def get_population_models(self):
        return self._ll_sim.get_population_models()

    def get_max_memory(self):
        return self._ll_sim.get_max_memory()

    def add_population_model(self, pop_model):
        self._population_models.append(pop_model)

    def set_num_loci(self, num_loci):
        self._num_loci = num_loci

    def set_scaled_recombination_rate(self, scaled_recombination_rate):
        self._scaled_recombination_rate = scaled_recombination_rate

    def set_effective_population_size(self, effective_population_size):
        self._effective_population_size = effective_population_size

    def set_random_seed(self, random_seed):
        self._random_seed = random_seed

    def set_segment_block_size(self, segment_block_size):
        self._segment_block_size = segment_block_size

    def set_avl_node_block_size(self, avl_node_block_size):
        self._avl_node_block_size = avl_node_block_size

    def set_node_mapping_block_size(self, node_mapping_block_size):
        self._node_mapping_block_size = node_mapping_block_size

    def set_coalescence_record_block_size(self, coalescence_record_block_size):
        self._coalescence_record_block_size = coalescence_record_block_size

    def set_max_memory(self, max_memory):
        """
        Sets the approximate maximum memory used by the simulation
        to the specified value.  This can be suffixed with
        K, M or G to specify units of Kibibytes, Mibibytes or Gibibytes.
        """
        s = max_memory
        d = {"K":2**10, "M":2**20, "G":2**30}
        multiplier = 1
        value = s
        if s.endswith(tuple(d.keys())):
            value = s[:-1]
            multiplier = d[s[-1]]
        n = int(value)
        self._max_memory = n * multiplier

    def _set_environment_defaults(self):
        """
        Sets sensible default values for the memory usage parameters.
        """
        # Set the block sizes using our estimates.
        n = self._sample_size
        m = self._num_loci
        # First check to make sure they are sane.
        if not isinstance(n, int):
            raise TypeError("Sample size must be an integer")
        if not isinstance(m, int):
            raise TypeError("Number of loci must be an integer")
        if n < 2:
            raise ValueError("Sample size must be >= 2")
        if m < 1:
            raise ValueError("Postive number of loci required")
        rho = 4 * self._scaled_recombination_rate * (m - 1)
        num_trees = min(m // 2, rho * harmonic_number(n - 1))
        b = 10 # Baseline maximum
        num_trees = max(b, int(num_trees))
        num_avl_nodes = max(b, 4 * n + num_trees)
        # TODO This is probably much too large now.
        num_segments = max(b, int(0.0125 * n  * rho))
        if self._avl_node_block_size is None:
            self._avl_node_block_size = num_avl_nodes
        if self._segment_block_size is None:
            self._segment_block_size = num_segments
        if self._node_mapping_block_size is None:
            self._node_mapping_block_size = num_trees
        if self._coalescence_record_block_size is None:
            memory = 16 * 2**10  # 16M
            # Each coalescence record is 32bytes
            self._coalescence_record_block_size = memory // 32
        if self._random_seed is None:
            self._random_seed = random.randint(0, 2**31 - 1)
        if self._max_memory is None:
            self._max_memory = 10 * 1024 * 1024 # 10MiB by default

    def run(self):
        """
        Runs the simulation until complete coalescence has occured.
        """
        models = [m.get_ll_model() for m in self._population_models]
        assert self._ll_sim is None
        self._set_environment_defaults()
        self._ll_sim = _msprime.Simulator(
            sample_size=self._sample_size,
            num_loci=self._num_loci,
            population_models=models,
            scaled_recombination_rate=self._scaled_recombination_rate,
            random_seed=self._random_seed,
            max_memory=self._max_memory,
            segment_block_size=self._segment_block_size,
            avl_node_block_size=self._avl_node_block_size,
            node_mapping_block_size=self._node_mapping_block_size,
            coalescence_record_block_size=self._coalescence_record_block_size)
        self._ll_sim.run()
        metadata = {
            "sample_size": self._sample_size,
            "num_loci": self._num_loci,
            "population_models": models,
            "random_seed": self._random_seed
        }
        records = self._ll_sim.get_coalescence_records()
        left, right, children, parent, time = records
        ts = TreeSequence(self._ll_sim.get_breakpoints(), left, right,
                children, parent, time, metadata, True)
        return ts

    def reset(self):
        """
        Resets the simulation so that we can perform another replicate.
        """
        self._ll_sim = None

class TreeSequence(object):

    def __init__(self, breakpoints, left, right, children, parent, time,
            metadata, sort_records=False):
        uint32 = "uint32"
        # TODO there is a quite a lot of copying going on here. Do
        # we need to do this?
        self._breakpoints = np.array(breakpoints, dtype=uint32)
        self._num_records = len(left)
        self._left  = np.array(left , dtype=uint32)
        self._right = np.array(right, dtype=uint32)
        self._children = np.array(children, dtype=uint32)
        self._parent = np.array(parent, dtype=uint32)
        self._time = np.array(time, dtype="double")
        self._sample_size = metadata["sample_size"]
        self._num_loci = metadata["num_loci"]
        self._metadata = dict(metadata)
        if sort_records:
            p = np.argsort(self._left)
            self._left = self._left[p]
            self._right = self._right[p]
            self._children = self._children[p]
            self._parent = self._parent[p]
            self._time = self._time[p]

    def print_state(self):
        print("metadata = ", self._metadata)
        for j in range(self._num_records):
            print(self._left[j], self._right[j], self._children[j],
                    self._parent[j], self._time[j], sep="\t")

    def dump(self, path):
        """
        Writes the tree sequence to the specified file path.
        """
        compression = None
        with h5py.File(path, "w") as f:
            f.attrs["version"] = "0.1"
            f.attrs["metadata"] = json.dumps(self._metadata)
            uint32 = "uint32"
            f.create_dataset(
                "breakpoints", data=self._breakpoints, dtype=uint32,
                compression=compression)
            records = f.create_group("records")
            records.create_dataset(
                "left", data=self._left, dtype=uint32, compression=compression)
            records.create_dataset(
                "right", data=self._right, dtype=uint32, compression=compression)
            records.create_dataset(
                "children", data=self._children, dtype=uint32, compression=compression)
            records.create_dataset(
                "parent", data=self._parent, dtype=uint32, compression=compression)
            records.create_dataset(
                "time", data=self._time, dtype="double", compression=compression)


    @classmethod
    def load(cls, path):
        with h5py.File(path, "r") as f:
            records = f["records"]
            metadata = json.loads(f.attrs["metadata"])
            ret = TreeSequence(f["breakpoints"], records["left"],
                    records["right"], records["children"],
                    records["parent"], records["time"], metadata)

        return ret

    def get_sample_size(self):
        return self._sample_size

    def get_num_loci(self):
        return self._num_loci

    def get_num_breakpoints(self):
        return len(self._breakpoints)

    def records(self):
        return zip(
            self._left, self._right, self._children, self._parent, self._time)

    def sparse_trees(self):
        n = self._sample_size
        pi = {}
        tau = {j:0 for j in range(1, n + 1)}
        l = 0
        last_l = 0
        live_segments = []
        # print("START")
        for l, r, children, parent, t in self.records():
            if last_l != l:
                # print("YIELDING TREE", len(live_segments))
                # for right, v in live_segments:
                    # print("\t", right, v)
                # q = 1
                # while q in pi:
                #     q = pi[q]
                # pi[q] = 0
                yield l - last_l, pi, tau
                # del pi[q]
                last_l = l
            heapq.heappush(live_segments, (r, (tuple(children), parent)))
            while live_segments[0][0] <= l:
                x, (other_children, p) = heapq.heappop(live_segments)
                # print("Popping off segment", x, children, p)
                for c in other_children:
                    del pi[c]
                del tau[p]
            pi[children[0]] = parent
            pi[children[1]] = parent
            tau[parent] = t
        # q = 1
        # while q in pi:
        #     q = pi[q]
        # pi[q] = 0
        yield self.get_num_loci() - l, pi, tau

    def diffs(self):
        n = self._sample_size
        left = 0
        used_records = collections.defaultdict(list)
        records_in = []
        for l, r, children, parent, t in self.records():
            if l != left:
                yield l - left, used_records[left], records_in
                del used_records[left]
                records_in = []
                left = l
            used_records[r].append((children, parent, t))
            records_in.append((children, parent, t))
        yield r - left, used_records[left], records_in

    def newick_trees(self, precision=3):
        # We want a top-down representation of the tree to make
        # generation of the Newick trees easier.
        c = {}
        branch_lengths = {}
        tau = {j: 0.0 for j in range(1, self._sample_size + 1)}
        pi = {}
        root = 1
        for length, records_out, records_in in self.diffs():
            # print("New tree:", length)
            # print("OUT")
            # for r in records_out:
            #     print("\t", r)
            # print("IN")
            # for r in records_in:
            #     print("\t", r)
            for children, parent, time in records_out:
                del c[parent]
                del tau[parent]
                for j in range(0, 2):
                    del pi[children[j]]
                    del branch_lengths[children[j]]
                if parent == root:
                    root = 1
            for children, parent, time in records_in:
                c[parent] = tuple(children)
                for j in range(0, 2):
                    pi[children[j]] = parent
                tau[parent] = time
                if time > tau[root]:
                    root = parent
            # Update the branch_lengths
            for children, parent, time in records_in:
                for j in range(0, 2):
                    s = "{0:.{1}f}".format(time - tau[children[j]], precision)
                    branch_lengths[children[j]] = s
            # print("Root = ", root)
            # print("child map:", c)
            # print("tau: ", tau)
            # print("b :", branch_lengths)
            assert len(c) == self._sample_size - 1
            assert len(tau) == 2 * self._sample_size - 1
            assert len(pi) == 2 * self._sample_size - 2
            assert len(branch_lengths) == 2 * self._sample_size - 2
            # TODO This root calculation above is wrong! Fix it!
            j = 1
            while j in pi:
                j = pi[j]
            root = j
            yield length, _tmp_build_newick(root, root, c, branch_lengths)


def _tmp_build_newick(node, root, tree, branch_lengths):
    if node in tree:
        c1, c2 = tree[node]
        s1 = _tmp_build_newick(c1, root, tree, branch_lengths)
        s2 = _tmp_build_newick(c2, root, tree, branch_lengths)
        if node == root:
            # The root node is treated differently
            s = "({0},{1});".format(s1, s2)
        else:
            s = "({0},{1}):{2}".format(
                s1, s2, branch_lengths[node])
    else:
        # Leaf node
        s = "{0}:{1}".format(node, branch_lengths[node])
    return s


class HaplotypeGenerator(object):
    """
    Class that takes a TreeFile and a recombination rate and builds a set
    of haplotypes consistent with the underlying trees.
    """
    def __init__(self, tree_file_name, mutation_rate, random_seed=None):
        seed = random_seed
        if random_seed is None:
            seed = random.randint(0, 2**31)
        self._ll_haplotype_generator = _msprime.HaplotypeGenerator(
                tree_file_name, mutation_rate=mutation_rate,
                random_seed=seed, max_haplotype_length=10000)

    def get_num_segregating_sites(self):
        return self._ll_haplotype_generator.get_haplotype_length()

    def get_haplotypes(self):
        # TODO this is pretty inefficient; should we do this down in C
        # or just offer a generator interface instead?
        bytes_haplotypes = self._ll_haplotype_generator.get_haplotypes()
        haps = [h.decode() for h in bytes_haplotypes[1:]]
        return haps


class PopulationModel(object):
    """
    Superclass of simulation population models.
    """
    def __init__(self, start_time):
        self.start_time = start_time

    def get_ll_model(self):
        """
        Returns the low-level model corresponding to this population
        model.
        """
        return self.__dict__

class ConstantPopulationModel(PopulationModel):
    """
    Class representing a constant-size population model. The size of this
    is expressed relative to the size of the population at sampling time.
    """
    def __init__(self, start_time, size):
        super(ConstantPopulationModel, self).__init__(start_time)
        self.size = size
        self.type = _msprime.POP_MODEL_CONSTANT


class ExponentialPopulationModel(PopulationModel):
    """
    Class representing an exponentially growing or shrinking population.
    TODO document model.
    """
    def __init__(self, start_time, alpha):
        super(ExponentialPopulationModel, self).__init__(start_time)
        self.alpha = alpha
        self.type = _msprime.POP_MODEL_EXPONENTIAL


