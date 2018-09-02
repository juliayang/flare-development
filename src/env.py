"""
Jon V
"""
import numpy as np
from math import exp
from numba import njit
from struc import Structure
import time


# get two body kernel between two environments
def two_body(env1, env2, d1, d2, sig, ls):
    return ChemicalEnvironment.two_body_jit(env1.bond_array,
                                            env1.bond_types,
                                            env2.bond_array,
                                            env2.bond_types,
                                            d1, d2, sig, ls)


def two_body_py(env1, env2, d1, d2, sig, ls):
    return ChemicalEnvironment.two_body_nojit(env1.bond_array,
                                              env1.bond_types,
                                              env2.bond_array,
                                              env2.bond_types,
                                              d1, d2, sig, ls)


class ChemicalEnvironment:

    def __init__(self, structure, atom):
        self.structure = structure

        bond_array, bond_types, etyps, ctyp =\
            self.get_atoms_within_cutoff(atom)

        self.bond_array = bond_array
        self.bond_types = bond_types
        self.etyps = etyps
        self.ctyp = ctyp

    @staticmethod
    def is_bond(species1, species2, bond):
        """Check if two species form a specified bond.

        :param species1: first species
        :type species1: str
        :param species2: second species
        :type species2: str
        :param bond: bond to be checked
        :type bond: list<str>
        :return: True or False
        :rtype: bool
        """

        return ([species1, species2] == bond) or ([species2, species1] == bond)

    def species_to_index(self, species1, species2):
        """Given two species, get the corresponding bond index.

        :param species1: first species
        :type species1: string
        :param species2: second species
        :type species2: string
        :param bond_list: all possible bonds
        :type bond_list: list
        :return: bond index
        :rtype: integer
        """

        for bond_index, bond in enumerate(self.structure.bond_list):
            if ChemicalEnvironment.is_bond(species1, species2, bond):
                return bond_index

    def get_local_atom_images(self, vec):
        """Get periodic images of an atom within the cutoff radius.

        :param vec: atomic position
        :type vec: nparray of shape (3,)
        :return: vectors and distances of atoms within cutoff radius
        :rtype: list of nparrays, list of floats
        """

        # get bravais coefficients
        coeff = np.matmul(self.structure.inv_lattice, vec)

        # get bravais coefficients for atoms within one super-super-cell
        coeffs = [[], [], []]
        for n in range(3):
            coeffs[n].append(coeff[n])
            coeffs[n].append(coeff[n]-1)
            coeffs[n].append(coeff[n]+1)
            coeffs[n].append(coeff[n]-2)
            coeffs[n].append(coeff[n]+2)

        # get vectors within cutoff
        vecs = []
        dists = []
        for m in range(len(coeffs[0])):
            for n in range(len(coeffs[1])):
                for p in range(len(coeffs[2])):
                    vec_curr = coeffs[0][m]*self.structure.vec1 +\
                               coeffs[1][n]*self.structure.vec2 +\
                               coeffs[2][p]*self.structure.vec3
                    dist = np.linalg.norm(vec_curr)

                    if dist < self.structure.cutoff:
                        vecs.append(vec_curr)
                        dists.append(dist)

        return vecs, dists

    # return information about atoms inside cutoff region
    def get_atoms_within_cutoff(self, atom):

        pos_atom = self.structure.positions[atom]  # position of central atom
        central_type = self.structure.species[atom]  # type of central atom

        bond_array = []
        bond_types = []
        environment_types = []

        # find all atoms and images in the neighborhood
        for n in range(len(self.structure.positions)):
            diff_curr = self.structure.positions[n] - pos_atom
            typ_curr = self.structure.species[n]
            bond_curr = self.species_to_index(central_type, typ_curr)

            # get images within cutoff
            vecs, dists = self.get_local_atom_images(diff_curr)

            for vec, dist in zip(vecs, dists):
                # ignore self interaction
                if dist != 0:
                    environment_types.append(typ_curr)
                    bond_array.append([dist, vec[0]/dist, vec[1]/dist,
                                       vec[2]/dist])
                    bond_types.append(bond_curr)

        bond_array = np.array(bond_array)
        bond_types = np.array(bond_types)
        return bond_array, bond_types, environment_types, central_type

    # jit function that computes two body kernel
    @staticmethod
    @njit
    def two_body_jit(bond_array_1, bond_types_1, bond_array_2,
                     bond_types_2, d1, d2, sig, ls):
        d = sig*sig/(ls*ls*ls*ls)
        e = ls*ls
        f = 1/(2*ls*ls)
        kern = 0

        x1_len = len(bond_types_1)
        x2_len = len(bond_types_2)

        for m in range(x1_len):
            r1 = bond_array_1[m, 0]
            coord1 = bond_array_1[m, d1]
            typ1 = bond_types_1[m]

            for n in range(x2_len):
                r2 = bond_array_2[n, 0]
                coord2 = bond_array_2[n, d2]
                typ2 = bond_types_2[n]

                # check that bonds match
                if typ1 == typ2:
                    rr = (r1-r2)*(r1-r2)
                    kern += d*exp(-f*rr)*coord1*coord2*(e-rr)

        return kern

    # for testing purposes, define python version of two body kernel
    @staticmethod
    def two_body_nojit(bond_array_1, bond_types_1, bond_array_2,
                       bond_types_2, d1, d2, sig, ls):
        d = sig*sig/(ls*ls*ls*ls)
        e = ls*ls
        f = 1/(2*ls*ls)
        kern = 0

        x1_len = len(bond_types_1)
        x2_len = len(bond_types_2)

        for m in range(x1_len):
            r1 = bond_array_1[m, 0]
            coord1 = bond_array_1[m, d1]
            typ1 = bond_types_1[m]

            for n in range(x2_len):
                r2 = bond_array_2[n, 0]
                coord2 = bond_array_2[n, d2]
                typ2 = bond_types_2[n]

                # check that bonds match
                if typ1 == typ2:
                    rr = (r1-r2)*(r1-r2)
                    kern += d*exp(-f*rr)*coord1*coord2*(e-rr)

        return kern


# testing ground (will be moved to test suite later)
if __name__ == '__main__':
    # create test structure
    positions = [np.array([0, 0, 0]), np.array([0.5, 0.5, 0.5])]
    species = ['B', 'A']
    cell = np.eye(3)
    cutoff = np.linalg.norm(np.array([0.5, 0.5, 0.5])) + 0.001
    test_structure = Structure(cell, species, positions, cutoff)

    # create environment
    atom = 0
    test_env = ChemicalEnvironment(test_structure, atom)

    # test species_to_bond
    is_bl_right = test_env.structure.bond_list ==\
        [['B', 'B'], ['B', 'A'], ['A', 'A']]
    assert(is_bl_right)

    # test is_bond (static method)
    assert(ChemicalEnvironment.is_bond('A', 'B', ['A', 'B']))
    assert(ChemicalEnvironment.is_bond('B', 'A', ['A', 'B']))
    assert(not ChemicalEnvironment.is_bond('C', 'A', ['A', 'B']))

    # test species_to_index
    assert(test_env.species_to_index('B', 'B') == 0)
    assert(test_env.species_to_index('B', 'A') == 1)
    assert(test_env.species_to_index('A', 'A') == 2)

    # test get_local_atom_images
    vec = np.array([0.5, 0.5, 0.5])
    vecs, dists = test_env.get_local_atom_images(vec)
    assert(len(dists) == 8)
    assert(len(vecs) == 8)

    # test get_atoms_within_cutoff
    atom = 0
    bond_array, bonds, environment_types, central_type =\
        test_env.get_atoms_within_cutoff(atom)

    assert(bond_array.shape[0] == 8)

    # test jit and python two body kernels
    def kernel_performance(env1, env2, d1, d2, sig, ls, kernel, its):
        # warm up jit
        time0 = time.time()
        kern_val = kernel(env1, env2, d1, d2, sig, ls)
        time1 = time.time()
        warm_up_time = time1 - time0

        # test run time performance
        time2 = time.time()
        for n in range(its):
            kernel(env1, env2, d1, d2, sig, ls)
        time3 = time.time()
        run_time = (time3 - time2) / its

        return kern_val, run_time, warm_up_time

    def get_jit_speedup(env1, env2, d1, d2, sig, ls, jit_kern, py_kern,
                        its):

        kern_val_jit, run_time_jit, warm_up_time_jit = \
            kernel_performance(env1, env2, d1, d2, sig, ls, jit_kern, its)

        kern_val_py, run_time_py, warm_up_time_py = \
            kernel_performance(env1, env2, d1, d2, sig, ls, py_kern, its)

        speed_up = run_time_py / run_time_jit

        return speed_up, kern_val_jit, kern_val_py, warm_up_time_jit,\
            warm_up_time_py

    # set up two test environments
    positions_1 = [np.array([0, 0, 0]), np.array([0.1, 0.2, 0.3])]
    species_1 = ['B', 'A']
    atom_1 = 0
    test_structure_1 = Structure(cell, species_1, positions_1, cutoff)
    env1 = ChemicalEnvironment(test_structure_1, atom_1)

    positions_2 = [np.array([0, 0, 0]), np.array([0.25, 0.3, 0.4])]
    species_2 = ['B', 'A']
    atom_2 = 0
    test_structure_2 = Structure(cell, species_2, positions_2, cutoff)
    env2 = ChemicalEnvironment(test_structure_2, atom_2)

    d1 = 1
    d2 = 1
    sig = 1
    ls = 1

    its = 100000

    # compare performance
    speed_up, kern_val_jit, kern_val_py, warm_up_time_jit, warm_up_time_py = \
        get_jit_speedup(env1, env2, d1, d2, sig, ls,
                        two_body, two_body_py, its)

    assert(kern_val_jit == kern_val_py)
    assert(speed_up > 1)

    print(speed_up)
