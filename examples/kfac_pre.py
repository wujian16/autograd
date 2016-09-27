from __future__ import division
import autograd.numpy as np
import autograd.numpy.random as npr
from autograd import grad
from autograd.scipy.misc import logsumexp
from autograd import grad_and_aux, val_and_grad
from autograd.util import getval
from data import load_mnist


### Vanilla neural net functions

def neural_net_predict(params, inputs):
    '''Deep neural network with muliticlass logistic predictions.'''
    return softmax(mlp(params, inputs))

def log_likelihood(params, inputs, targets):
    '''Like log_posterior in neural_net.py, but no prior (regularizer) term.'''
    logprobs = neural_net_predict(params, inputs)
    return np.sum(logprobs * targets)

def mlp(params, inputs):
    '''A multi-layer perceptron with a linear last layer.'''
    for W, b in params:
        outputs = np.dot(inputs, W) + b
        inputs = np.tanh(outputs)
    return outputs

def softmax(inputs):
    '''Log softmax, the canonical link function for logistic regression.'''
    return inputs - logsumexp(inputs, axis=1, kepdims=True)

def init_random_params(scale, layer_sizes):
    """Build a list of (weights, biases) tuples,
       one for each layer in the net."""
    return [(scale * npr.randn(m, n), scale * npr.randn(n))
            for m, n in zip(layer_sizes[:-1], layer_sizes[1:])]

### General utility functions

def sample_discrete_from_log(logprobs):
    '''Given an NxD array where each row stores the log probabilities of a
       finite density, return the NxD array of one-hot encoded samples from
       those densities.'''
    probs = np.exp(logprobs)
    cumvals = np.cumsum(probs, axis=1)
    indices = np.sum(npr.rand(logprobs.shape[0], 1) > cumvals, axis=1)
    return np.eye(logprobs.shape[1])[indices]

### K-FAC utility functions

# First, we need to augment the neural net computation to collect the required
# statistics, namely samples of the activations and samples of the gradients of
# those activations under random targets generated by the model. To collect the
# gradients, we use an autograd trick: we add extra bias terms (set to zero)
# and compute gradients with respect to them.

# NOTE: this implementation currently does two forward passes on each minibatch
# while only one is necessary; the forward pass used to compute the training
# objective can be reused with the model-generated targets.

def neural_net_predict_and_activations(extra_biases, params, inputs):
    '''Like the neural_net_predict function in neural_net.py, but
       (1) adds extra biases and (2) also returns all computed activations.'''
    all_activations = [inputs]
    for (W, b), extra_bias in zip(params, extra_biases):
        s = np.dot(all_activations[-1], W) + b + extra_bias
        all_activations.append(np.tanh(s))
    logprobs = s - logsumexp(s, axis=1, keepdims=True)
    return logprobs, all_activations[:-1]

def model_predictive_log_likelihood(extra_biases, params, inputs):
    '''Computes log_likelihood on targets sampled from the model.
       Also returns all computed activations.'''
    logprobs, activations = neural_net_predict_and_activations(
        extra_biases, params, inputs)
    model_sampled_targets = sample_discrete_from_log(getval(logprobs))
    return np.sum(logprobs * model_sampled_targets), activations

def collect_activations_and_grad_samples(params, inputs, num_samples):
    '''Collects the statistics necessary to estimate the approximate Fisher
       information matrix used in K-FAC.'''
    inputs = inputs[npr.choice(inputs.shape[0], size=num_samples)]
    extra_biases = [np.zeros((inputs.shape[0], b.shape[0])) for W, b in params]
    gradfun = grad_and_aux(model_predictive_log_likelihood)
    g_samples, a_samples = gradfun(extra_biases, params, inputs)
    return a_samples, g_samples

### Bookkeeping for samples

# These functions are just to help collect samples across multiple iterations.

def append_samples(all_samples, new_samples):
    '''Appends the newly-collected layerwise samples to the rest of the samples.
       Both all_samples and new_samples are lists of length num_layers,
       all_samples[0] is a list of all the samples for layer 0,
       all_samples[1] is a list of all the samples for layer 1, etc.
    '''
    for layer_samples, new_layer_samples in zip(all_samples, new_samples):
        layer_samples.append(new_layer_samples)

def init_sample_lists(layer_sizes):
    return [[[] for _ in layer_sizes[:-1]] for _ in range(2)]

### Bookkeeping for kron factor estimates

# These functions are for turning the collected samples into estimates of the
# Kronecker factors that we use to define the K-FAC preconditioner.

append_homog_coord = lambda x: np.hstack((x, np.ones((x.shape[0], 1))))
identity = lambda x: x

def estimate_block_factors(all_samples, append_homog=False):
    '''Given a list of samples for each layer, estimates the second moment from
       the samples.'''
    num_samples = sum(samples.shape[0] for samples in all_samples[0])
    homog = append_homog_coord if append_homog else identity
    sumsq = lambda samples: np.dot(samples.T, samples)
    layer_sumsq = lambda layer_samples: \
        sum(map(sumsq, map(homog, layer_samples))) / num_samples
    return map(layer_sumsq, all_samples)

def update_factor_estimates(old_estimates, samples, eps):
    As, Gs = old_estimates
    a_samples, g_samples = samples
    Ahats = estimate_block_factors(a_samples, append_homog=True)
    Ghats = estimate_block_factors(g_samples)
    update = lambda old, new: eps*old + (1.-eps)*new
    return map(update, As, Ahats), map(update, Gs, Ghats)

def init_factor_estimates(layer_sizes):
    layer_sizes = np.array(layer_sizes)
    return map(np.eye, layer_sizes[:-1] + 1), map(np.eye, layer_sizes[1:])

### Computing and applying the preconditioner

# These functions compute the inverses of the Kronecker factors and apply the
# K-FAC preconditioner to parameter updates.

def compute_precond(factor_estimates, lmbda):
    inv = lambda X: np.linalg.inv(X + lmbda*np.eye(X.shape[0]))
    layer_inv = lambda layer_factors: map(inv, layer_factors)
    return map(layer_inv, factor_estimates)

def apply_preconditioner(precond, gradient):
    def apply_block(Ainv, Ginv, W_grad, b_grad):
        Wb_grad = np.vstack((W_grad, b_grad))
        Wb_natgrad = np.dot(Ainv, np.dot(Wb_grad, Ginv))
        return Wb_natgrad[:-1], Wb_natgrad[-1]

    factors = zip(*precond)
    return [apply_block(A, G, W, b) for (A,G), (W,b) in zip(factors, gradient)]

### K-FAC-pre (simplified preconditioned SGD version)

# K-FAC is specific to fully-connected layers, so its interface needs to know
# more than the other optimizers in optimizers.py. In particular, it only works
# when the parameters are a list of weights and biases, it needs to know the
# layer sizes, it needs ot know the likelihood model on the last layer (logistic
# regression here), and it needs to have direct access to the training data.

def kfac(objective, get_batch, layer_sizes, init_params, step_size, num_iters,
         num_samples, sample_period, reestimate_period, update_precond_period,
         lmbda, eps):

    ## initialize

    samples = init_sample_lists(layer_sizes)
    factors = init_factor_estimates(layer_sizes)
    precond = compute_precond(factors, lmbda=lmbda)

    ## helper functions

    def collect_samples(params, i):
        new_samples = collect_activations_and_grad_samples(
            params, get_batch(i), num_samples)
        map(append_samples, samples, new_samples)

    def update_params(params, natgrad, step_size):
      return [(W - step_size*dW, b - step_size*db)
              for (W, b), (dW, db) in zip(params, natgrad)]

    objective_grad = val_and_grad(objective)

    ## main loop

    params = init_params
    for i in range(num_iters):
        val, gradient = objective_grad(params, i)

        if (i+1) % sample_period == 0:
            collect_samples(params, i)

        if (i+1) % reestimate_period == 0:
            factors = update_factor_estimates(factors, samples, eps)
            samples = init_sample_lists(layer_sizes)

        if (i+1) % update_precond_period == 0:
            precond = compute_precond(factors, lmbda=lmbda)

        cond = lambda X: np.linalg.cond(X + lmbda * np.eye(X.shape[0]))
        print map(lambda lst: map(cond, lst), factors)

        natgrad = apply_preconditioner(precond, gradient)
        params = update_params(params, natgrad, step_size)

    return params

### script

if __name__ == '__main__':
    npr.seed(0)

    # Model parameters
    layer_sizes = [784, 200, 100, 10]

    # Training parameters
    param_scale = 0.1
    batch_size = 1024

    # Load data
    N, train_images, train_labels, test_images,  test_labels = load_mnist()
    train_images = npr.permutation(train_images)
    train_images += 1e-2 * npr.randn(*train_images.shape)

    # initialize parameters
    init_params = init_random_params(param_scale, layer_sizes)

    # Divide data into batches
    num_batches = int(np.ceil(len(train_images) / batch_size))

    def batch_indices(itr):
        idx = itr % num_batches
        return slice(idx * batch_size, (idx+1) * batch_size)

    get_batch = lambda itr: train_images[batch_indices(itr)]

    # Define training objective
    def objective(params, itr):
        idx = batch_indices(itr)
        return -log_likelihood(params, train_images[idx], train_labels[idx])

    # Optimize!
    optimized_params = kfac(
        objective, get_batch, layer_sizes, init_params, step_size=1e-3,
        num_iters=1000, lmbda=0., eps=0.05, num_samples=10*batch_size,
        sample_period=1e4, reestimate_period=1e4, update_precond_period=1e4)


# NOTE: right factor can blow up because we have an over-parameterized logistic
# and hence the Fisher is rank-deficient (all-ones is in its null space). The
# left factor can also blow up because of the background in the images.

# TODO test against true Fisher
# TODO get regular sgd working in this file just like in other file
# TODO maybe fix overparameterization of last layer

# TODO handle other likelihoods
# TODO adapt lmbda
# TODO add num_samples
