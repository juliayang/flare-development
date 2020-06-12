import inspect
import json
import logging
import math
import pickle
import time

import multiprocessing as mp
import numpy as np

from collections import Counter
from copy import deepcopy
from numpy.random import random
from scipy.linalg import solve_triangular
from scipy.optimize import minimize
from typing import List, Callable, Union

from flare.env import AtomicEnvironment
from flare.gp import GaussianProcess
from flare.gp_algebra import get_like_from_mats, get_neg_like_grad, \
    force_force_vector, energy_force_vector, get_force_block, \
    get_ky_mat_update, _global_training_data, _global_training_labels, \
    _global_training_structures, _global_energy_labels, get_Ky_mat, \
    get_kernel_vector, en_kern_vec
from flare.kernels.utils import str_to_kernel_set, from_mask_to_args, kernel_str_to_array
from flare.output import Output, set_logger
from flare.parameters import Parameters
from flare.struc import Structure
from flare.utils.element_coder import NumpyEncoder, Z_to_element


class RobustBayesianCommitteeMachine(GaussianProcess):
    """Gaussian process force field. Implementation is based on Algorithm 2.1
    (pg. 19) of "Gaussian Processes for Machine Learning" by Rasmussen and
    Williams.

    Args:
        kernels (list, optional): Determine the type of kernels. Example:
            ['2', '3'], ['2', '3', 'mb'], ['2']. Defaults to ['2', '3']
        component (str, optional): Determine single- ("sc") or multi-
            component ("mc") kernel to use. Defaults to "mc"
        hyps (np.ndarray, optional): Hyperparameters of the GP.
        cutoffs (Dict, optional): Cutoffs of the GP kernel.
        hyp_labels (List, optional): List of hyperparameter labels. Defaults
            to None.
        opt_algorithm (str, optional): Hyperparameter optimization algorithm.
            Defaults to 'L-BFGS-B'.
        maxiter (int, optional): Maximum number of iterations of the
            hyperparameter optimization algorithm. Defaults to 10.
        parallel (bool, optional): If True, the covariance matrix K of the GP is
            computed in parallel. Defaults to False.
        n_cpus (int, optional): Number of cpus used for parallel
            calculations. Defaults to 1 (serial)
        n_sample (int, optional): Size of submatrix to use when parallelizing
            predictions.
        output (Output, optional): Output object used to dump hyperparameters
            during optimization. Defaults to None.
        hyps_mask (dict, optional): hyps_mask can set up which hyper parameter
            is used for what interaction. Details see kernels/mc_sephyps.py
        name (str, optional): Name for the GP instance.
    """

    def __init__(self, n_experts, ndata_per_expert, prior_variance,
                 kernels: list = ['two', 'three'],
                 component: str = 'mc',
                 hyps: 'ndarray' = None, cutoffs={},
                 hyps_mask: dict = {},
                 hyp_labels: List = None, opt_algorithm: str = 'L-BFGS-B',
                 maxiter: int = 10, parallel: bool = False,
                 per_atom_par: bool = True, n_cpus: int = 1,
                 n_sample: int = 100, output: Output = None,
                 name="default_gp",
                 energy_noise: float = 0.01, **kwargs,):

        self.n_experts = n_experts
        self.prior_variance = prior_variance
        self.log_prior_var = np.log(prior_variance)
        self.ndata_per_expert = ndata_per_expert
        self.current_expert = 0

        GaussianProcess.__init__(self, kernels, component,
                     hyps, cutoffs, hyps_mask, hyp_labels, opt_algorithm,
                     maxiter, parallel, per_atom_par, n_cpus, n_sample, output,
                     name, energy_noise, **kwargs,)

        self.training_data = []
        self.training_labels = []  # Forces acting on central atoms
        self.training_labels_np = [] # np.empty(0, )
        self.n_envs_prev = [] # len(self.training_data)

        # Attributes to accomodate energy labels:
        self.training_structures = []  # Environments of each structure
        self.energy_labels = []  # Energies of training structures
        self.energy_labels_np = []
        self.all_labels = []

        # Parameters set during training
        self.ky_mat = []
        self.force_block = []
        self.energy_block = []
        self.force_energy_block = []
        self.l_mat = []
        self.alpha = []
        self.ky_mat_inv = []
        self.likelihood = []

        for i in range(self.n_experts):
            self.add_container()


    def find_expert_to_add(self):

        expert_id = self.current_expert
        if self.n_envs_prev[expert_id] > self.ndata_per_expert:
            self.current_expert += 1
            expert_id = self.current_expert

        return expert_id

    def add_container(self):

        self.training_data += [[]]
        self.training_labels += [[]]
        self.training_labels_np += [np.empty(0, )]
        self.n_envs_prev += [0]

        self.training_structures += [[]]  # Environments of each structure
        self.energy_labels += [[]]  # Energies of training structures
        self.energy_labels_np += [np.empty(0, )]
        self.all_labels += [np.empty(0, )]

        self.ky_mat += [None]
        self.force_block += [None]
        self.energy_block += [None]
        self.force_energy_block += [None]
        self.l_mat += [None]
        self.alpha += [None]
        self.ky_mat_inv += [None]

        self.likelihood += [None]


    def update_db(self, struc: Structure, forces: List,
                  custom_range: List[int] = (), energy: float = None,
                  expert_id: int = None):
        """Given a structure and forces, add local environments from the
        structure to the training set of the GP. If energy is given, add the
        entire structure to the training set.

        Args:
            struc (Structure): Input structure. Local environments of atoms
                in this structure will be added to the training set of the GP.

            forces (np.ndarray): Forces on atoms in the structure.

            custom_range (List[int]): Indices of atoms whose local
                environments will be added to the training set of the GP.

            energy (float): Energy of the structure.
        """

        if expert_id is None:
            expert_id = self.find_expert_to_add()

        if expert_id >= self.n_experts:
            self.add_container()
            self.n_experts += 1

        # By default, use all atoms in the structure
        noa = len(struc.positions)
        update_indices = custom_range or list(range(noa))

        # If forces are given, update the environment list.
        if forces is not None:
            for atom in update_indices:
                env_curr = \
                    AtomicEnvironment(struc, atom, self.cutoffs,
                                      cutoffs_mask=self.hyps_mask)
                forces_curr = np.array(forces[atom])

                self.training_data[expert_id].append(env_curr)
                self.training_labels[expert_id].append(forces_curr)
                self.n_envs_prev[expert_id] += 1

            self.training_labels_np[expert_id] = np.hstack(self.training_labels[expert_id])

        # If an energy is given, update the structure list.
        if energy is not None:
            structure_list = []  # Populate with all environments of the struc
            for atom in range(noa):
                env_curr = \
                    AtomicEnvironment(struc, atom, self.cutoffs,
                                      cutoffs_mask=self.hyps_mask)
                structure_list.append(env_curr)

            self.energy_labels[expert_id].append(energy)
            self.training_structures[expert_id].append(structure_list)
            self.energy_labels_np[expert_id] = np.array(self.energy_labels[expert_id])

        # update list of all labels
        self.all_labels[expert_id] = np.concatenate((self.training_labels_np[expert_id],
                                                     self.energy_labels_np[expert_id]))

    def add_one_env(self, env: AtomicEnvironment,
                    force, train: bool = False, expert_id = None, **kwargs):
        """Add a single local environment to the training set of the GP.

        Args:
            env (AtomicEnvironment): Local environment to be added to the
                training set of the GP.
            force (np.ndarray): Force on the central atom of the local
                environment in the form of a 3-component Numpy array
                containing the x, y, and z components.
            train (bool): If True, the GP is trained after the local
                environment is added.
        """

        if expert_id is None:
            expert_id = self.find_expert_to_add()

        if expert_id >= self.n_experts:
            self.add_container()
            self.n_experts += 1

        self.logger.debug(f"add environment to Expert {expert_id}")

        self.training_data[expert_id].append(env)
        self.training_labels[expert_id].append(force)
        self.training_labels_np[expert_id] = np.hstack(self.training_labels[expert_id])
        self.n_envs_prev[expert_id] += 1

        # update list of all labels
        self.all_labels[expert_id] = np.concatenate((self.training_labels_np[expert_id],
                                                     self.energy_labels_np[expert_id]))

        if train:
            self.train(**kwargs)

    def train(self, logger=None, custom_bounds=None,
              grad_tol: float = 1e-4,
              x_tol: float = 1e-5,
              line_steps: int = 20,
              print_progress: bool = False):
        """Train Gaussian Process model on training data. Tunes the
        hyperparameters to maximize the likelihood, then computes L and alpha
        (related to the covariance matrix of the training set).

        Args:
            logger (logging.Logger): logger object specifying where to write the
                progress of the optimization.
            custom_bounds (np.ndarray): Custom bounds on the hyperparameters.
            grad_tol (float): Tolerance of the hyperparameter gradient that
                determines when hyperparameter optimization is terminated.
            x_tol (float): Tolerance on the x values used to decide when
                Nelder-Mead hyperparameter optimization is terminated.
            line_steps (int): Maximum number of line steps for L-BFGS
                hyperparameter optimization.
        """

        verbose = "warning"
        if print_progress:
            verbose = "info"
        if logger is None:
            logger = set_logger("gp_algebra", stream=True,
                                fileout=True, verbose=verbose)
        else:
            logger.setlevel(getattr(logging, verbose.upper()))

        disp = False # print_progress

        if len(self.training_data) == 0 or len(self.training_labels) == 0:
            raise Warning("You are attempting to train a GP with no "
                          "training data. Add environments and forces "
                          "to the GP and try again.")

        self.sync_data()

        x_0 = self.hyps

        args = (self.n_experts, self.name, self.kernel_grad,
                logger, self.cutoffs, self.hyps_mask,
                self.n_cpus, self.n_sample)

        func = rbcm_get_neg_like_grad


        res = None

        if self.opt_algorithm == 'L-BFGS-B':

            # bound signal noise below to avoid overfitting
            if self.bounds is None:
                bounds = np.array([(1e-6, np.inf)] * len(x_0))
                bounds[-1, 0] = 1e-3
            else:
                bounds = self.bounds

            # Catch linear algebra errors and switch to BFGS if necessary
            try:
                res = minimize(func, x_0, args,
                               method='L-BFGS-B', jac=True, bounds=bounds,
                               options={'disp': disp, 'gtol': grad_tol,
                                        'maxls': line_steps,
                                        'maxiter': self.maxiter})
            except np.linalg.LinAlgError:
                self.logger.warning("Algorithm for L-BFGS-B failed. Changing to "
                               "BFGS for remainder of run.")
                self.opt_algorithm = 'BFGS'

        if custom_bounds is not None:
            res = minimize(func, x_0, args,
                           method='L-BFGS-B', jac=True, bounds=custom_bounds,
                           options={'disp': disp, 'gtol': grad_tol,
                                    'maxls': line_steps,
                                    'maxiter': self.maxiter})

        elif self.opt_algorithm == 'BFGS':
            res = minimize(func, x_0, args,
                           method='BFGS', jac=True,
                           options={'disp': disp, 'gtol': grad_tol,
                                    'maxiter': self.maxiter})

        if res is None:
            raise RuntimeError("Optimization failed for some reason.")
        self.hyps = res.x
        self.set_L_alpha()
        self.total_likelihood = -res.fun
        self.total_likelihood_gradient = -res.jac

        return res

    def check_L_alpha(self):
        """
        Check that the alpha vector is up to date with the training set. If
        not, update_L_alpha is called.
        """

        self.sync_data()

        # Check that alpha is up to date with training set
        for i in range(self.n_experts):
            size3 = len(self.training_data[i])*3

            # If model is empty, then just return
            if size3 == 0:
                return

            if (self.alpha is None):
                self.update_L_alpha(i)
            elif (size3 > self.alpha[i].shape[0]):
                self.update_L_alpha(i)
            elif (size3 != self.alpha[i].shape[0]):
                self.set_L_alpha_part(i)

    def predict(self, x_t: AtomicEnvironment, d: int) -> [float, float]:
        """
        Predict a force component of the central atom of a local environment.

        Args:
            x_t (AtomicEnvironment): Input local environment.
            d (int): Force component to be predicted (1 is x, 2 is y, and
                3 is z).

        Return:
            (float, float): Mean and epistemic variance of the prediction.
        """

        assert (d in [1, 2, 3]), "d should be 1, 2, or 3"

        # Kernel vector allows for evaluation of atomic environments.
        if self.parallel and not self.per_atom_par:
            n_cpus = self.n_cpus
        else:
            n_cpus = 1

        self.sync_data()

        k_v = []
        for i in range(self.n_experts):
            k_v += \
                [get_kernel_vector(f"{self.name}_{i}", self.kernel,
                                   self.energy_force_kernel,
                                   x_t, d, self.hyps, cutoffs=self.cutoffs,
                                   hyps_mask=self.hyps_mask, n_cpus=n_cpus,
                                   n_sample=self.n_sample)]

        # Guarantee that alpha is up to date with training set
        self.check_L_alpha()

        # get predictive mean
        variance_rbcm = 0
        mean = 0
        var = 0
        beta = 0
        for i in range(self.n_experts):
            mean_k = np.matmul(k_v[i], self.alpha[i])

            # get predictive variance without cholesky (possibly faster)
            # pass args to kernel based on if mult. hyperparameters in use
            args = from_mask_to_args(self.hyps, self.cutoffs, self.hyps_mask)

            self_kern = self.kernel(x_t, x_t, d, d, *args)
            var_k = self_kern - np.matmul(np.matmul(k_v[i], self.ky_mat_inv[i]), k_v[i])
            beta_k = 0.5*(self.log_prior_var - np.log(var_k))

            mean += beta_k / var_k * mean_k
            var += beta_k / var_k
            beta += beta_k

        var += (1-beta)/self.prior_variance
        pred_var = 1.0/var
        pred_mean = pred_var * mean

        return pred_mean, pred_var

    # def predict_local_energy(self, x_t: AtomicEnvironment) -> float:
    #     """Predict the local energy of a local environment.

    #     Args:
    #         x_t (AtomicEnvironment): Input local environment.

    #     Return:
    #         float: Local energy predicted by the GP.
    #     """

    #     if self.parallel and not self.per_atom_par:
    #         n_cpus = self.n_cpus
    #     else:
    #         n_cpus = 1

    #     _global_training_data[self.name] = self.training_data
    #     _global_training_labels[self.name] = self.training_labels_np

    #     k_v = en_kern_vec(self.name, self.energy_force_kernel,
    #                       self.energy_kernel,
    #                       x_t, self.hyps, cutoffs=self.cutoffs,
    #                       hyps_mask=self.hyps_mask, n_cpus=n_cpus,
    #                       n_sample=self.n_sample)

    #     pred_mean = np.matmul(k_v, self.alpha)

    #     return pred_mean

    # def predict_local_energy_and_var(self, x_t: AtomicEnvironment):
    #     """Predict the local energy of a local environment and its
    #     uncertainty.

    #     Args:
    #         x_t (AtomicEnvironment): Input local environment.

    #     Return:
    #         (float, float): Mean and predictive variance predicted by the GP.
    #     """

    #     if self.parallel and not self.per_atom_par:
    #         n_cpus = self.n_cpus
    #     else:
    #         n_cpus = 1

    #     # get kernel vector
    #     k_v = en_kern_vec(self.name, self.energy_force_kernel,
    #                       self.energy_kernel,
    #                       x_t, self.hyps, cutoffs=self.cutoffs,
    #                       hyps_mask=self.hyps_mask, n_cpus=n_cpus,
    #                       n_sample=self.n_sample)

    #     # get predictive mean
    #     pred_mean = np.matmul(k_v, self.alpha)

    #     # get predictive variance
    #     v_vec = solve_triangular(self.l_mat, k_v, lower=True)
    #     args = from_mask_to_args(self.hyps, self.cutoffs, self.hyps_mask)

    #     self_kern = self.energy_kernel(x_t, x_t, *args)

    #     pred_var = self_kern - np.matmul(v_vec, v_vec)

    #     return pred_mean, pred_var

    def set_L_alpha(self):

        self.sync_data()

        for expert_id in range(self.n_experts):

            self.logger.debug(f"compute L_alpha for {expert_id}")
            time0 = time.time()

            ky_mat = get_Ky_mat(self.hyps, f"{self.name}_{expert_id}", self.kernel,
                       self.energy_kernel, self.energy_force_kernel,
                       self.energy_noise,
                       self.cutoffs, self.hyps_mask,
                       self.n_cpus, self.n_sample)

            self.compute_one_matrices(ky_mat, expert_id)

            self.likelihood[expert_id] = get_like_from_mats(self.ky_mat[expert_id],
                                                            self.l_mat[expert_id],
                                                            self.alpha[expert_id],
                                                            f"{self.name}_{expert_id}")
            self.logger.debug(f"compute_L_alpha {time.time()-time0}")

        self.total_likelihood = np.sum(self.likelihood)

    def set_L_alpha_part(self, expert_id):
        """
        Invert the covariance matrix, setting L (a lower triangular
        matrix s.t. L L^T = (K + sig_n^2 I)) and alpha, the inverse
        covariance matrix multiplied by the vector of training labels.
        The forces and variances are later obtained using alpha.
        """

        self.sync_one_data(expert_id)

        ky_mat = \
            get_Ky_mat(self.hyps, f"{self.name}_{expert_id}", self.kernel,
                       self.energy_kernel, self.energy_force_kernel,
                       self.energy_noise,
                       cutoffs=self.cutoffs, hyps_mask=self.hyps_mask,
                       n_cpus=self.n_cpus, n_sample=self.n_sample)

        self.compute_one_matrices(ky_mat, expert_id)

        self.likelihood[expert_id] = get_like_from_mats(self.ky_mat[expert_id],
                                                        self.l_mat[expert_id],
                                                        self.alpha[expert_id],
                                                        f"{self.name}_{expert_id}")

    def sync_data(self):
        for i in range(self.n_experts):
            self.sync_one_data(i)

    def sync_one_data(self, expert_id):

        """ Reset global variables. """
        if len(self.training_data) > expert_id:
            _global_training_data[f"{self.name}_{expert_id}"] = self.training_data[expert_id]
            _global_training_labels[f"{self.name}_{expert_id}"] = self.training_labels_np[expert_id]
            _global_training_structures[f"{self.name}_{expert_id}"] = self.training_structures[expert_id]
            _global_energy_labels[f"{self.name}_{expert_id}"] = self.energy_labels_np[expert_id]

    def write_model(self, name: str, format: str = 'json'):

        if np.sum(self.n_envs_prev) > 5000:

            np.savez(f"{name}_ky_mat.npz", self.ky_mat)
            self.ky_mat_file = f"{name}_ky_mat.npz"

            temp_ky_mat = self.ky_mat
            temp_l_mat = self.l_mat
            temp_alpha = self.alpha
            temp_ky_mat_inv = self.ky_mat_inv

            self.ky_mat = None
            self.l_mat = None
            self.alpha = None
            self.ky_mat_inv = None

        GaussianProcess.write_model(self, name, format)

        self.ky_mat = temp_ky_mat
        self.l_mat = temp_l_mat
        self.alpha = temp_alpha
        self.ky_mat_inv = temp_ky_mat_inv

    def update_L_alpha(self, expert_id):
        """
        Update the GP's L matrix and alpha vector without recalculating
        the entire covariance matrix K.
        """

        # Set L matrix and alpha if set_L_alpha has not been called yet
        if self.l_mat[expert_id] is None or np.array(self.ky_mat[expert_id]) is np.array(None):
            self.set_L_alpha_part(expert_id)
            return

        self.sync_data(expert_id)

        ky_mat = get_ky_mat_update(self.ky_mat[expert_id],
                                   self.n_envs_prev[expert_id],
                                   self.hyps,
                                   f"{self.name}_{expert_id}", self.kernel,
                                   self.energy_kernel,
                                   self.energy_force_kernel,
                                   self.energy_noise,
                                   cutoffs=self.cutoffs,
                                   hyps_mask=self.hyps_mask,
                                   n_cpus=self.n_cpus,
                                   n_sample=self.n_sample)

        self.compute_one_matrices(ky_mat, expert_id)

    def compute_one_matrices(self, ky_mat, expert_id):
        """
        When covariance matrix is known, reconstruct other matrices.
        Used in re-loading large GPs.
        :return:
        """

        l_mat = np.linalg.cholesky(ky_mat)
        l_mat_inv = np.linalg.inv(l_mat)
        ky_mat_inv = l_mat_inv.T @ l_mat_inv
        alpha = np.matmul(ky_mat_inv, self.all_labels[expert_id])

        self.ky_mat[expert_id] = ky_mat
        self.l_mat[expert_id] = l_mat
        self.alpha[expert_id] = alpha
        self.ky_mat_inv[expert_id] = ky_mat_inv
        self.n_envs_prev[expert_id] = len(self.training_data[expert_id])

    @property
    def training_statistics(self) -> dict:
        """
        Return a dictionary with statistics about the current training data.
        Useful for quickly summarizing info about the GP.
        :return:
        """

        data = {}

        # Count all of the present species in the atomic env. data
        present_species = []
        for i in range(self.n_experts):
            data[f'N_{i}'] = self.n_envs_prev[i]
            for env, _ in zip(self.training_data[i], self.training_labels[i]):
                present_species.append(Z_to_element(env.structure.coded_species[
                    env.atom]))

        # Summarize the relevant information
        data['species'] = list(set(present_species))
        data['envs_by_species'] = dict(Counter(present_species))

        return data


    def write_model(self, name: str, format: str = 'json'):
        """
        Write model in a variety of formats to a file for later re-use.
        Args:
            name (str): Output name.
            format (str): Output format.
        """

        supported_formats = ['json', 'pickle', 'binary']

        logger = self.logger
        self.logger = None

        if format.lower() == 'json':
            raise ValueError("Output format not supported: try from "
                             "{}".format(supported_formats))
            # with open(f'{name}.json', 'w') as f:
            #     json.dump(self.as_dict(), f, cls=NumpyEncoder)

        elif format.lower() == 'pickle' or format.lower() == 'binary':
            with open(f'{name}.pickle', 'wb') as f:
                pickle.dump(self, f)

        else:
            raise ValueError("Output format not supported: try from "
                             "{}".format(supported_formats))
        self.logger = logger

    @staticmethod
    def from_file(filename: str, format: str = ''):
        """
        One-line convenience method to load a GP from a file stored using
        write_file

        Args:
            filename (str): path to GP model
            format (str): json or pickle if format is not in filename
        :return:
        """

        if '.json' in filename or 'json' in format:
            raise ValueError("Output format not supported: try from "
                             "{}".format(supported_formats))
            # with open(filename, 'r') as f:
            #     gp_model = GaussianProcess.from_dict(json.loads(f.readline()))

        elif '.pickle' in filename or 'pickle' in format:
            with open(filename, 'rb') as f:

                gp_model = pickle.load(f)

                GaussianProcess.backward_arguments(
                    gp_model.__dict__, gp_model.__dict__)

                GaussianProcess.backward_attributes(gp_model.__dict__)

        else:
            raise ValueError("Warning: Format unspecieified or file is not "
                             ".json or .pickle format.")

        # # TO DO, be careful of this one
        # gp_model.check_instantiation()

        return gp_model


def rbcm_get_neg_like_grad(hyps, n_experts, name, kernel_grad, logger, cutoffs, hyps_mask, n_cpus, n_sample):


    like = 0
    like_grad = None

    time0 = time.time()
    for i in range(n_experts):
        like_, like_grad_ = get_neg_like_grad(hyps, f"{name}_{i}", kernel_grad, logger,
                                              cutoffs, hyps_mask, n_cpus,
                                              n_sample)
        like += like_
        if (like_grad is None):
            like_grad = like_grad_
        else:
            like_grad += like_grad_

    logger.info('')
    logger.info(f'Hyperparameters: {list(hyps)}')
    logger.info(f'Total Likelihood: {-like}')
    logger.info(f'Total Likelihood Gradient: {list(like_grad)}')
    logger.info(f"one step {time.time()-time0}")

    return like, like_grad

