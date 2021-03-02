# Copyright 2021 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""FISTA implementation in JAX."""

from typing import Callable
from typing import Optional

import jax
import jax.numpy as jnp
from jax.scipy.sparse import linalg as sparse_linalg

from jaxopt.loop import while_loop


def _linesearch(curr_x,
                curr_stepsize,
                params_f,
                value_f,
                grad_f,
                fun_f,
                prox_g,
                max_iter,
                unroll,
                stepfactor):
  """Backtracking line search."""

  def cond_fun(stepsize):
    next_x = prox_g(curr_x - stepsize * grad_f, stepsize)
    diff = next_x - curr_x
    sqdist = jnp.sum(diff ** 2)
    value_F = fun_f(next_x, params_f)
    value_Q = value_f + jnp.sum(diff * grad_f) + 0.5 / stepsize * sqdist
    return value_F > value_Q

  def body_fun(stepsize):
    return stepsize * stepfactor

  # Currently, we never jit when unrolling, since jitting a huge graph is slow.
  # In the future, we will improve loop.while_loop similarly to
  # https://github.com/google-research/ott/blob/master/ott/core/fixed_point_loop.py
  jit = not unroll

  return while_loop(cond_fun=cond_fun, body_fun=body_fun,
                    init_val=curr_stepsize, max_iter=max_iter, unroll=unroll,
                    jit=jit)


def make_fista_body_fun(init: jnp.ndarray,
                        fun_f: Callable,
                        params_f: jnp.ndarray,
                        prox_g: Optional[Callable] = None,
                        max_iter_linesearch: int = 10,
                        acceleration: bool = True,
                        unroll_linesearch: bool = False,
                        stepfactor: float = 0.5):
  """Create a body_fun for performing one iteration of FISTA."""

  if prox_g is None:
    prox_g = lambda x, alpha: x

  value_and_grad_fun = jax.value_and_grad(fun_f)
  grad_fun = jax.grad(fun_f)

  def error_fun(curr_x, params_f):
    grad_f = grad_fun(curr_x, params_f)
    diff_x = prox_g(curr_x - grad_f, 1.0) - curr_x
    return jnp.sqrt(jnp.sum(diff_x ** 2))

  def _iter(curr_x, curr_stepsize):
    value_f, grad_f = value_and_grad_fun(curr_x, params_f)
    next_stepsize = _linesearch(curr_x, curr_stepsize, params_f, value_f,
                                grad_f, fun_f, prox_g, max_iter_linesearch,
                                unroll_linesearch, stepfactor)
    next_x = prox_g(curr_x - next_stepsize * grad_f, next_stepsize)

    # If step size becomes too small, we restart it to 1.0.
    # Otherwise, we attempt to increase it.
    next_stepsize = jnp.where(next_stepsize <= 1e-6,
                              1.0,
                              next_stepsize / stepfactor)

    return next_x, next_stepsize

  def body_fun_ista(args):
    curr_x, curr_stepsize, _ = args
    next_x, next_stepsize = _iter(curr_x, curr_stepsize)
    next_error = error_fun(next_x, params_f)
    return next_x, next_stepsize, next_error

  def body_fun_fista(args):
    curr_x, curr_y, curr_t, curr_stepsize, _ = args
    next_x, next_stepsize = _iter(curr_y, curr_stepsize)
    next_t = 0.5 * (1 + jnp.sqrt(1 + 4 * curr_t ** 2))
    next_y = next_x + (curr_t - 1) / next_t * (next_x - curr_x)
    next_error = error_fun(next_x, params_f)
    return next_x, next_y, next_t, next_stepsize, next_error

  if acceleration:
    return body_fun_fista
  else:
    return body_fun_ista


def _implicit_diff_prox_vjp(sol, fun_f, params_f, prox_g, v):
  grad_fun = jax.grad(fun_f)
  pt = sol - grad_fun(sol, params_f)
  alpha = 1.0
  if prox_g is not None:
    _, vjp_fun_g = jax.vjp(prox_g, pt, alpha)
  _, vjp_fun_f = jax.vjp(grad_fun, sol, params_f)

  def f_hvp(u):
    dir_deriv = lambda x: jnp.vdot(grad_fun(x, params_f), u)
    return jax.grad(dir_deriv)(sol)

  def matvec(u):
    if prox_g is None:
      # Multiply with B, where B = Hessian of fun_f w.r.t x
      return f_hvp(u)
    else:
      tmp = vjp_fun_g(u)[0]
      # Multiply with M^T = (AB)^T + I - A^T = B^T A^T + I - A^T
      # where A = Jacobian of prox_g in first argument
      return jnp.transpose(f_hvp(tmp) + u - tmp)

  u = sparse_linalg.cg(matvec, v)[0]

  if prox_g is not None:
    # Multiply with -AC, where C = Jacobian of grad_f in params_f.
    tmp = vjp_fun_g(u)[0]
    return -vjp_fun_f(tmp)[1]
  else:
    # Multiply with -C
    return -vjp_fun_f(u)[1]


def _fista(init, fun_f, params_f, prox_g, max_iter, max_iter_linesearch,
           tol, acceleration, verbose, unroll):

  def cond_fun(args):
    error = args[-1]
    if verbose:
      print(error)
    return error > tol

  body_fun = make_fista_body_fun(init, fun_f, params_f, prox_g,
                                 max_iter_linesearch, acceleration,
                                 unroll_linesearch=unroll)

  if acceleration:
    # curr_x, curr_y, curr_t, curr_stepsize, error
    args = (init, init, 1.0, 1.0, 1e6)
  else:
    # curr_x, curr_stepsize, error
    args = (init, 1.0, 1e6)

  # Currently, we never jit when unrolling, since jitting a huge graph is slow.
  # In the future, we will improve loop.while_loop similarly to
  # https://github.com/google-research/ott/blob/master/ott/core/fixed_point_loop.py
  jit = not unroll

  return while_loop(cond_fun=cond_fun, body_fun=body_fun, init_val=args,
                    max_iter=max_iter, unroll=unroll, jit=jit)[0]


def _fista_fwd(init, fun_f, params_f, prox_g, max_iter, max_iter_linesearch,
               tol, acceleration, verbose, unroll):
  sol = _fista(init, fun_f, params_f, prox_g, max_iter, max_iter_linesearch,
               tol, acceleration, verbose, unroll)
  return sol, (params_f, sol)


def _fista_bwd(fun_f, prox_g, max_iter, max_iter_linesearch, tol, acceleration,
               verbose, unroll, res, g):
  params_f, sol = res
  vjp_params_f = _implicit_diff_prox_vjp(sol, fun_f, params_f, prox_g, g)
  return (None, vjp_params_f)


def fista(fun_f: Callable,
          init: jnp.ndarray,
          params_f: Optional[jnp.ndarray] = None,
          prox_g: Optional[Callable] = None,
          max_iter: int = 500,
          max_iter_linesearch: int = 10,
          tol: float = 1e-3,
          acceleration: bool = True,
          verbose: int = 0,
          implicit_diff: bool = False) -> jnp.ndarray:
  """Solves argmin_x fun_f(x, params_f) + fun_g(x, params_g) using FISTA.

  The stopping criterion is

  ||x - prox_g(x - grad(f)(x, params_f), params_g)||_2 <= tol.

  Args:
    init: initialization to use for x (jnp.ndarray).
    fun_f: a smooth function of the form fun_f(x, params_f).
    params_f: parameters to use for fun_f above.
    prox_g: proximity operator associated with the function g.
    max_iter: maximum number of FISTA iterations.
    max_iter_linesearch: maximum number of iterations to use in the line search.
    tol: tolerance to use.
    acceleration: whether to use acceleration (FISTA) or not (ISTA).
    verbose: verbosity level.
    implicit_diff: whether to use implicit differentiation or not.
      implicit_diff=False will trigger loop unrolling.
  Returns:
    Approximate solution to the argmin problem (jnp.ndarray).
  """
  if implicit_diff:
    # We use implicit differentiation.
    fun = jax.custom_vjp(_fista, nondiff_argnums=(1, 3, 4, 5, 6, 7, 8, 9))
    fun.defvjp(_fista_fwd, _fista_bwd)
  else:
    # We leave differentiation to JAX.
    fun = _fista

  return fun(init=init, fun_f=fun_f, params_f=params_f, prox_g=prox_g,
             max_iter=max_iter, max_iter_linesearch=max_iter_linesearch,
             tol=tol, acceleration=acceleration, verbose=verbose,
             unroll=not implicit_diff)
