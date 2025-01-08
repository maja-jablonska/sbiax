from typing import Tuple, Callable, Optional
import blackjax.progress_bar
import jax
import jax.random as jr
import jax.numpy as jnp
from jaxtyping import PRNGKeyArray, Array, Float
import blackjax
from tensorflow_probability.substrates.jax.distributions import Distribution


def nuts_sample(
    key: PRNGKeyArray, 
    log_prob_fn: Callable, 
    prior: Distribution, 
    n_samples: int = 100_000, 
    n_chains: int = 1,
    n_warmup_steps: int = 1000,
    initial_state: Optional[Float[Array, "#n p"]] = None,
    sampling_kwargs: Optional[dict] = None
) -> Tuple[Float[Array, "#n p"], Float[Array, "#n 1"]]:
    """
    Runs NUTS (No-U-Turn Sampler) to sample from a posterior distribution using JAX.

    This function performs sampling using the NUTS algorithm, implemented via BlackJAX, 
    with an initial warm-up phase for tuning parameters. It uses the window adaptation 
    process to adjust the parameters during warm-up and runs the sampler in parallel 
    for multiple chains.

    Args:
        key: A JAX `PRNGKeyArray`.
        log_prob_fn: A callable representing the log probability function of the 
            posterior distribution. This function should take a set of parameters 
            as input and return their log probability (`Callable`).
        prior: A `tensorflow_probability` `Distribution` object representing the 
            prior distribution from which the initial parameter values are sampled (`Distribution`).
        n_samples: The number of posterior samples to generate (`int`). Default is 100,000.

    Returns:
        Tuple[`Array`, `Array`]:
            - The first array contains the sampled parameter positions from the NUTS algorithm 
            with shape `(n_samples,)` for one chain.
            - The second array contains the log densities (log posterior probabilities) corresponding 
            to each sampled position with shape `(n_samples,)`.

    Process:
        1. The prior distribution is used to sample initial parameter values.
        2. The NUTS sampler is tuned and adapted during the warm-up phase using `blackjax.window_adaptation`.
        3. After warm-up, the function performs sampling over `n_samples` using the NUTS kernel 
        provided by `blackjax.nuts.build_kernel`, running the sampler for one or more chains.
        4. The function returns the positions (sampled parameter values) and their associated 
        log densities from the posterior distribution.

    Example:
        ```python
        import jax
        import jax.random as jr
        from tensorflow_probability.substrates.jax.distributions import Normal
        from sbiax.inference import nuts_sample

        def log_prob_fn(params):
            # Typically this takes in a datavector of some kind!
            return -0.5 * jnp.sum(params ** 2)  

        key = jr.key(0)
        prior = Normal(0, 1)
        samples, log_densities = nuts_sample(key, log_prob_fn, prior)
        ```
    """
    if prior is not None:
        assert isinstance(prior, Distribution), (
            "Only tfp distributions are compatible currently."
        )

    key, init_key, warmup_key, sample_key = jr.split(key, 4)

    def init_param_fn(seed):
        return prior.sample(seed=seed)

    warmup = blackjax.window_adaptation(blackjax.nuts, log_prob_fn)

    init_keys = jr.split(init_key, n_chains)

    if initial_state is not None:
        initial_params = initial_state
    else:
        initial_params = jax.vmap(init_param_fn)(init_keys)

    @jax.vmap
    def call_warmup(seed, param):
        (initial_states, tuned_params), _ = warmup.run(seed, param, n_warmup_steps)
        return initial_states, tuned_params

    warmup_keys = jr.split(warmup_key, n_chains)
    initial_states, tuned_params = jax.jit(call_warmup)(warmup_keys, initial_params)

    def inference_loop_multiple_chains(
        key, 
        initial_states, 
        tuned_params, 
        log_prob_fn, 
        n_samples, 
        num_chains
    ):
        kernel = blackjax.nuts.build_kernel()

        def step_fn(key, state, **params):
            return kernel(key, state, log_prob_fn, **params)

        def one_step(states, i):
            keys = jr.split(jr.fold_in(key, i), num_chains)
            states, infos = jax.vmap(step_fn)(keys, states, **tuned_params)
            return states, (states, infos)

        _, (states, infos) = jax.lax.scan(
            one_step, initial_states, jnp.arange(n_samples)
        )
        return states, infos

    states, infos = inference_loop_multiple_chains(
        sample_key, 
        initial_states, 
        tuned_params, 
        log_prob_fn, 
        n_samples, 
        n_chains
    )
    
    return (
        states.position.transpose(1, 0, 2), 
        states.logdensity.transpose(1, 0)
    )