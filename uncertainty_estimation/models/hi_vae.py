"""
Module providing an implementation of a Heterogenous-Incomplete Variational Auto-Encoder (HI-VAE).
"""

# STD
import abc
from collections import Counter
from typing import Tuple, List, Optional, Set
import math

# EXT
import numpy as np
import torch
from torch import nn
import torch.distributions as dist
import torch.nn.functional as F

# PROJECT
from uncertainty_estimation.models.vae import VAE
from uncertainty_estimation.models.info import (
    DEFAULT_LEARNING_RATE,
    DEFAULT_RECONSTR_ERROR_WEIGHT,
)

# CONSTANTS
AVAILABLE_TYPES = {"real", "positive_real", "count", "categorical", "ordinal"}

# TYPES
# A list of tuples specifying the types of input features
# Just name of the distribution and optionally the min and max value for ordinal / categorical features
# e.g. [("real", None, None), ("categorical", None, 5), ("ordinal", 1, 3)]
FeatTypes = List[Tuple[str, int, int]]


# TODO: Group variables of the same type together to make computations more efficient

# -------------------------------------------------- Encoder -----------------------------------------------------------


class HIEncoder(nn.Module):
    """
    The encoder module, which encodes an input into the latent space.

    Parameters
    ----------
    hidden_sizes: List[int]
        A list with the sizes of the hidden layers.
    input_size: int
        The input dimensionality.
    latent_dim: int
        The size of the latent space.
    """

    def __init__(
        self,
        hidden_sizes: List[int],
        latent_dim: int,
        n_mix_components: int,
        feat_types: FeatTypes,
    ):
        super().__init__()

        only_types = list(zip(*feat_types))[0]

        assert set(only_types) & AVAILABLE_TYPES == set(only_types), (
            "Unknown feature type declared. Must "
            "be in ['real', 'positive_real', "
            "'count', 'categorical', 'ordinal']."
        )

        self.n_mix_components = n_mix_components
        self.feat_types = feat_types
        self.encoded_input_size = self.get_encoded_input_size(feat_types)

        architecture = [self.encoded_input_size] + hidden_sizes
        self.layers = []

        self.real_batch_norm = torch.nn.BatchNorm1d(
            num_features=len(feat_types), affine=False,
        )
        self.real_batch_norm.register_forward_pre_hook(self.batch_norm_reset_hook)

        self.mixture_model = nn.Linear(self.encoded_input_size, self.n_mix_components)

        for l, (in_dim, out_dim) in enumerate(zip(architecture[:-1], architecture[1:])):
            self.layers.append(nn.Linear(in_dim, out_dim))
            self.layers.append(nn.LeakyReLU())

        self.hidden = nn.Sequential(*self.layers)
        self.mean = nn.Linear(architecture[-1] + self.n_mix_components, latent_dim)
        self.var = nn.Linear(architecture[-1] + self.n_mix_components, latent_dim)

        # Separate networks predicting the moments of the latent space prior
        self.p_mean = nn.Linear(self.n_mix_components, latent_dim)
        self.p_var = nn.Linear(self.n_mix_components, latent_dim)

    @staticmethod
    def batch_norm_reset_hook(module, *args):
        module.num_batches_tracked = torch.zeros(1)
        module.running_mean = torch.zeros(module.running_mean.shape)
        module.running_var = torch.ones(module.running_var.shape)

    @staticmethod
    def get_encoded_input_size(feat_types: FeatTypes) -> int:
        """
        Get the number of features after encoding categorical and ordinal features.
        """
        input_size = 0

        for feat_type, feat_min, feat_max in feat_types:

            if feat_type == "categorical":
                input_size += int(feat_max) + 1

            elif feat_type == "ordinal":
                input_size += int(feat_max - feat_min + 1)

            else:
                input_size += 1

        return input_size

    def categorical_encode(
        self, input_tensor: torch.Tensor
    ) -> Tuple[torch.Tensor, List[str]]:
        """
        Create one-hot / thermometer encodings for categorical / ordinal variables.
        """
        encoded_input_tensor = torch.empty(input_tensor.shape[0], 0)
        batch_size = input_tensor.shape[0]

        for dim, (feat_type, feat_min, feat_max) in enumerate(self.feat_types):

            # Use one-hot encoding
            if feat_type == "categorical":
                num_options = int(feat_max) + 1
                one_hot_encoding = F.one_hot(
                    input_tensor[:, dim].long(), num_classes=num_options
                ).float()
                encoded_input_tensor = torch.cat(
                    [encoded_input_tensor, one_hot_encoding], dim=1
                )

            # Use thermometer encoding
            elif feat_type == "ordinal":
                num_values = int(feat_max - feat_min + 1)
                thermometer_encoding = torch.cat(
                    [torch.arange(0, num_values).unsqueeze(0)] * batch_size, dim=0
                )
                cmp = input_tensor[:, dim].unsqueeze(1).repeat(1, num_values)
                thermometer_encoding = (thermometer_encoding <= cmp).float()
                encoded_input_tensor = torch.cat(
                    [encoded_input_tensor, thermometer_encoding], dim=1
                )

            # Simply add the feature dim, untouched
            else:
                encoded_input_tensor = torch.cat(
                    [encoded_input_tensor, input_tensor[:, dim].unsqueeze(1)], dim=1
                )

        return encoded_input_tensor

    def normalize(
        self, input_tensor: torch.Tensor
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        only_types = list(zip(*self.feat_types))[0]

        observed_mask = ~torch.isnan(
            input_tensor
        )  # Remember which values where observed
        input_tensor[~observed_mask] = 0  # Replace missing values with 0

        # Transform log-normal and count features
        log_transform_mask = torch.BoolTensor(
            [feat_type in ("positive_real", "count") for feat_type in only_types]
        )
        log_transform_indices = torch.arange(0, input_tensor.shape[1])[
            log_transform_mask
        ]
        input_tensor[:, log_transform_mask] = torch.log(
            F.relu(torch.index_select(input_tensor, dim=1, index=log_transform_indices))
            + 1e-8
        )

        # Normalize real features
        real_mask = torch.BoolTensor(
            [feat_type not in ("real", "positive_real") for feat_type in only_types]
        )
        real_indices = torch.arange(0, input_tensor.shape[1])[real_mask]

        normed_input = self.real_batch_norm(input_tensor)
        # Recover values for non-real variables
        normed_input[:, real_mask] = torch.index_select(
            input_tensor, dim=1, index=real_indices
        )

        return normed_input, observed_mask

    def forward(
        self, input_tensor: torch.Tensor
    ) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]:
        """Perform forward pass of encoder. Returns mean and standard deviation corresponding to
        an independent Normal distribution.

        Parameters
        ----------
        input_tensor: torch.Tensor
            The input to the encoder.
        """
        input_tensor, observed_mask = self.normalize(input_tensor)

        input_tensor = self.categorical_encode(input_tensor)
        mix_component_dists = self.sample_mix_components(input_tensor)
        mix_components = F.one_hot(torch.argmax(mix_component_dists, dim=1)).float()

        h = self.hidden(input_tensor)
        h = torch.cat([h, mix_components], dim=1)

        mean = self.mean(h)
        var = F.softplus(self.var(h))
        std = torch.sqrt(var)

        return mean, std, mix_component_dists, observed_mask

    def sample_mix_components(self, input_tensor: torch.Tensor) -> torch.Tensor:
        # Create a categorical distribution with equal probabilities
        pi = self.mixture_model(input_tensor)
        mix_components_dists = F.gumbel_softmax(pi, dim=1)

        return mix_components_dists


# -------------------------------------------------- Decoder -----------------------------------------------------------


class VarDecoder(nn.Module, abc.ABC):
    """
    Abstract variable decoder class that forces subclasses to implement some common methods.
    """

    # TODO: Add forward() here

    def __init__(
        self, hidden_size: int, feat_type: Tuple[str, Optional[int], Optional[int]],
    ):
        super().__init__()

        self.hidden_size = hidden_size
        self.feat_type = feat_type

    @abc.abstractmethod
    def reconstruction_error(
        self, input_tensor: torch.Tensor, hidden: torch.Tensor, dim: int
    ) -> torch.Tensor:
        """
        Compute the log probability of the original data sample under p(x|z).

        Parameters
        ----------
        input_tensor: torch.Tensor
            Original data sample.
        latent_tensor: torch.Tensor
            A sample from the latent space, which has to be decoded.

        Returns
        -------
        reconstr_error: torch.Tensor
            Log probability of the input under the decoder's distribution.
        """
        ...


class NormalDecoder(VarDecoder):
    """
    Decode a variable that is normally distributed.
    """

    def __init__(
        self,
        hidden_size: int,
        feat_type: Tuple[str, Optional[int], Optional[int]],
        encoder_batch_norm: torch.nn.BatchNorm1d,
    ):
        super().__init__(hidden_size, feat_type)

        self.mean = nn.Linear(hidden_size, 1)
        self.var = nn.Linear(hidden_size, 1)
        self.encoder_sb = encoder_batch_norm

    def forward(self, hidden: torch.Tensor, dim: int, reconstruction_mode: str):
        mean = self.mean(hidden).squeeze(1)

        if reconstruction_mode == "mode":
            return mean.squeeze(1)

        else:
            var = F.softplus(self.var(hidden))
            std = torch.sqrt(var).squeeze(1)
            eps = torch.randn(mean.shape)
            sample = mean + eps * std

            return sample

    def reconstruction_error(
        self, input_tensor: torch.Tensor, hidden: torch.Tensor, dim: int
    ) -> torch.Tensor:
        """
        Compute the log probability of the original data sample under p(x|z).

        Parameters
        ----------
        input_tensor: torch.Tensor
            Original feature.
        latent_tensor: torch.Tensor
            A sample from the latent space, which has to be decoded.

        Returns
        -------
        reconstr_error: torch.Tensor
            Log probability of the input under the decoder's distribution.
        """
        running_std = torch.sqrt(self.encoder_sb.running_var[dim])
        running_mean = self.encoder_sb.running_mean[dim]
        mean = self.mean(hidden)
        mean = mean * running_std + running_mean  # Batch de-normalization
        var = F.softplus(self.var(hidden))
        std = torch.sqrt(var) * running_std

        # calculating losses
        distribution = dist.independent.Independent(dist.normal.Normal(mean, std), 1)

        input_tensor = input_tensor * running_std + running_mean
        reconstr_error = -distribution.log_prob(input_tensor)

        return reconstr_error


class LogNormalDecoder(NormalDecoder):
    """
    Decode a variable that is distributed according to a log-normal distribution.
    """

    def forward(self, hidden: torch.Tensor, dim: int, reconstruction_mode: str):
        return torch.exp(super().forward(hidden, dim, reconstruction_mode))


class PoissonDecoder(VarDecoder):
    """
    Decode a variable that is distributed according to a Poisson distribution.
    """

    def __init__(
        self,
        hidden_size: int,
        feat_type: Tuple[str, Optional[int], Optional[int]],
        **unused,
    ):
        super().__init__(hidden_size, feat_type)

        self.lambda_ = nn.Linear(hidden_size, 1)

    def forward(self, hidden: torch.Tensor, dim: int, reconstruction_mode: str):
        lambda_ = F.softplus(self.lambda_(hidden)).float()

        if reconstruction_mode == "mode":
            return lambda_.int().squeeze(1)

        else:
            distribution = dist.poisson.Poisson(lambda_)
            sample = distribution.sample().squeeze(1)

            return sample

    def reconstruction_error(
        self, input_tensor: torch.Tensor, hidden: torch.Tensor, dim: int
    ) -> torch.Tensor:
        lambda_ = F.softplus(self.lambda_(hidden)).int().squeeze()
        err = F.poisson_nll_loss(input_tensor, lambda_)

        return err


class CategoricalDecoder(VarDecoder):
    """
    Decode a categorical variable.
    """

    def __init__(
        self,
        hidden_size: int,
        feat_type: Tuple[str, Optional[int], Optional[int]],
        **unused,
    ):
        super().__init__(hidden_size, feat_type)

        self.linear = nn.Linear(hidden_size, int(self.feat_type[2]) + 1)

    def forward(self, hidden: torch.Tensor, dim: int, reconstruction_mode: str):
        dist = self.linear(hidden)

        if reconstruction_mode == "mode":
            return torch.argmax(dist, dim=1)

        else:
            sample = torch.argmax(F.gumbel_softmax(dist, dim=1), dim=1)

            return sample

    def reconstruction_error(
        self, input_tensor: torch.Tensor, hidden: torch.Tensor, dim: int
    ) -> torch.Tensor:

        dists = self.linear(hidden)
        dists = F.softmax(dists, dim=1)
        err = -F.cross_entropy(dists, target=input_tensor.long())

        return err


class OrdinalDecoder(VarDecoder):
    """
    Decode an ordinal variable.
    """

    def __init__(
        self,
        hidden_size: int,
        feat_type: Tuple[str, Optional[int], Optional[int]],
        **unused,
    ):
        super().__init__(hidden_size, feat_type)

        self.thresholds = nn.Linear(
            hidden_size, int(self.feat_type[2] - self.feat_type[1] + 1)
        )
        self.region = nn.Linear(hidden_size, 1)

    def get_ordinal_probs(self, hidden: torch.Tensor):
        region = F.softplus(self.region(hidden))

        # Thresholds might not be ordered, use a cumulative sum
        thresholds = F.softplus(self.thresholds(hidden))
        thresholds = torch.cumsum(thresholds, dim=1)

        # Calculate probs that the predicted region is enclosed by threshold
        # p(x<=r|z)
        threshold_probs = 1 / (1 + torch.exp(-(thresholds - region)))

        # Now calculate probability for different ordinals
        # p(x=r|z) = p(x<=r|x) - p(x<=r-1|x)
        cmp = torch.roll(threshold_probs, shifts=1, dims=1)
        cmp[:, 0] = 0
        ordinal_probs = threshold_probs - cmp
        ordinal_probs = F.softmax(ordinal_probs, dim=1)

        return ordinal_probs

    def forward(self, hidden: torch.Tensor, dim: int, reconstruction_mode: str):
        ordinal_probs = self.get_ordinal_probs(hidden)

        if reconstruction_mode == "mode":
            return torch.argmax(ordinal_probs, dim=1)

        else:
            sample = torch.argmax(F.gumbel_softmax(ordinal_probs, dim=1), dim=1)

            return sample

    def reconstruction_error(
        self, input_tensor: torch.Tensor, hidden: torch.Tensor, dim: int
    ):
        # Sometimes the lowest ordinal will be > 0, but the input dropout replaces missing with 0. Because this messes
        # up the indexing this value is replaced here. Because components of the reconstruction loss corresponding to
        # non-observed feature will be ignored later, this doesn't matter.
        if self.feat_type[1] > 0:
            input_tensor[input_tensor == 0] = self.feat_type[1]
            input_tensor = (
                input_tensor - self.feat_type[1]
            )  # Shift labels so indexing matches up with tensor

        ordinal_probs = self.get_ordinal_probs(hidden)
        err = -F.cross_entropy(ordinal_probs, target=input_tensor.long())

        return err


class HIDecoder(nn.Module):
    """
    The decoder module, which decodes a sample from the latent space back to the space of
    the input data.

    Parameters
    ----------
    hidden_sizes: List[int]
        A list with the sizes of the hidden layers.
    input_size: int
        The dimensionality of the input
    latent_dim: int
        The size of the latent space.
    """

    def __init__(
        self,
        hidden_sizes: List[int],
        latent_dim: int,
        n_mix_components: int,
        feat_types: FeatTypes,
        encoder_batch_norm: torch.nn.BatchNorm1d,
    ):
        super().__init__()

        self.decoding_models = {
            "real": NormalDecoder,
            "positive_real": LogNormalDecoder,
            "count": PoissonDecoder,
            "categorical": CategoricalDecoder,
            "ordinal": OrdinalDecoder,
        }

        self.feat_types = feat_types
        self.n_mix_components = n_mix_components
        self.encoder_bn = encoder_batch_norm

        architecture = [latent_dim] + hidden_sizes
        self.layers = []

        for l, (in_dim, out_dim) in enumerate(zip(architecture[:-1], architecture[1:])):
            self.layers.append(nn.Linear(in_dim, out_dim))
            self.layers.append(nn.LeakyReLU())

        self.hidden = nn.Sequential(*self.layers)

        # Initialize all the output networks
        self.decoding_models = [
            self.decoding_models[feat_type[0]](
                architecture[-1] + n_mix_components,
                feat_type,
                encoder_batch_norm=encoder_batch_norm,
            )
            for feat_type in feat_types
        ]

    def forward(
        self,
        latent_tensor: torch.Tensor,
        mix_components: torch.Tensor,
        reconstruction_mode: str = "mode",
    ) -> torch.Tensor:
        h = self.hidden(latent_tensor)
        h = torch.cat([h, mix_components], dim=1)
        reconstruction = torch.zeros(
            (latent_tensor.shape[0], len(self.decoding_models))
        )

        for dim, (feat_type, decoding_func) in enumerate(
            zip(self.feat_types, self.decoding_models)
        ):
            reconstruction[:, dim] = decoding_func(h, dim, reconstruction_mode)

        return reconstruction

    def reconstruction_error(
        self,
        input_tensor: torch.Tensor,
        latent_tensor: torch.Tensor,
        mix_components: torch.Tensor,
        observed_mask: torch.Tensor,
    ) -> torch.Tensor:
        h = self.hidden(latent_tensor)
        h = torch.cat([h, mix_components], dim=1)
        reconstruction_loss = torch.zeros(input_tensor.shape)

        for feat_num, decoding_model in enumerate(self.decoding_models):
            reconstruction_loss[:, feat_num] = decoding_model.reconstruction_error(
                input_tensor[:, feat_num], h, dim=feat_num
            )

        reconstruction_loss[
            ~observed_mask
        ] = 0  # Only compute reconstruction loss for observed vars
        reconstruction_loss = reconstruction_loss.sum(dim=1)

        return reconstruction_loss


# ------------------------------------------------- Full model ---------------------------------------------------------


class HIVAEModule(nn.Module):
    """
    Module for the Heterogenous-Incomplete Variational Autoencoder.
    """

    def __init__(
        self,
        hidden_sizes: List[int],
        latent_dim: int,
        n_mix_components: int,
        feat_types: FeatTypes,
    ):
        super().__init__()
        self.latent_dim = latent_dim
        self.n_mix_components = n_mix_components

        self.encoder = HIEncoder(hidden_sizes, latent_dim, n_mix_components, feat_types)
        self.decoder = HIDecoder(
            hidden_sizes,
            latent_dim,
            n_mix_components,
            feat_types,
            encoder_batch_norm=self.encoder.real_batch_norm,
        )

    def forward(
        self,
        input_tensor: torch.Tensor,
        reconstr_error_weight: float,
        reconstruction_mode: bool = "sample",
    ) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:

        assert reconstruction_mode in ("mode", "sample"), (
            f"reconstruction_mode has to be either 'mode' or 'sample', "
            f"{reconstruction_mode} found."
        )

        input_tensor = input_tensor.float()

        # Encoding
        mean, std, mix_components_dists, observed_mask = self.encoder(input_tensor)
        eps = torch.randn(mean.shape)
        latent_tensor = mean + eps * std

        # Decoding
        mix_components = F.one_hot(torch.argmax(mix_components_dists, dim=1)).float()
        input_tensor, _ = self.encoder.normalize(
            input_tensor
        )  # Make sure necessary variables are normalized
        reconstr_error = self.decoder.reconstruction_error(
            input_tensor, latent_tensor, mix_components, observed_mask
        )

        # Calculating the KL divergence of the two independent Gaussians (closed-form solution)
        p_mean = self.encoder.p_mean(mix_components)
        p_var = F.softplus(self.encoder.p_var(mix_components))
        log_p_var = torch.log(p_var)
        log_var = torch.log(std.pow(2))
        kl = 0.5 * self.latent_dim + 0.5 * torch.sum(
            torch.exp(log_var - log_p_var)
            + (p_mean - mean).pow(2) / p_var
            - log_var
            + log_p_var,
            dim=1,
        )

        # KL(q(s_n|x_n^o)||p(s_n)
        kl_s = F.cross_entropy(
            mix_components_dists, target=torch.argmax(mix_components_dists, dim=1)
        ) + math.log(self.n_mix_components)

        average_negative_elbo = torch.mean(
            reconstr_error_weight * reconstr_error + kl + kl_s
        )

        return reconstr_error, kl, average_negative_elbo

    def reconstruct(
        self, input_tensor: torch.Tensor, reconstruction_mode: bool = "sample"
    ) -> torch.Tensor:

        input_tensor = input_tensor.float()

        # Encoding
        mean, std, observed_mask = self.encoder(input_tensor)
        eps = torch.randn(mean.shape)
        latent_tensor = mean + eps * std

        # Reconstruction
        reconstruction = self.decoder(latent_tensor, reconstruction_mode)

        return reconstruction


class HIVAE(VAE):
    def __init__(
        self,
        hidden_sizes: List[int],
        input_size: int,
        latent_dim: int,
        n_mix_components: int,
        feat_types: FeatTypes,
        lr: float = DEFAULT_LEARNING_RATE,
        reconstr_error_weight: float = DEFAULT_RECONSTR_ERROR_WEIGHT,
    ):
        super().__init__(
            hidden_sizes, input_size, latent_dim, lr, reconstr_error_weight
        )
        self.n_mix_components = n_mix_components

        self.model = HIVAEModule(hidden_sizes, latent_dim, n_mix_components, feat_types)

    def reconstruct(
        self, input_tensor: torch.Tensor, reconstruction_mode: bool = "sample"
    ) -> torch.Tensor:
        return self.model.reconstruct(input_tensor, reconstruction_mode)


# ---------------------------------------------- Helper functions ------------------------------------------------------


def infer_types(
    X: np.array,
    feat_names: List[str],
    unique_thresh: int = 20,
    count_kws: Set[str] = frozenset({"num", "count"}),
    ordinal_kws: Set[str] = frozenset({"scale", "Verbal", "Eyes", "Motor", "GCS"}),
) -> FeatTypes:
    """
    A basic function to infer the types from a data set automatically.
    """
    feat_types = []

    for dim, feat_name in enumerate(feat_names):
        feat_values = X[:, dim]
        feat_values = feat_values[~np.isnan(feat_values)]

        # Distinguish real-valued from integer-valued
        if all(feat_values.astype(int) == feat_values):

            # Count features
            if any(kw in feat_name for kw in count_kws):
                feat_type = "count"

            # Ordinal features
            elif any(kw in feat_name for kw in ordinal_kws):
                feat_type = "ordinal"

            # Categorical
            elif len(set(feat_values)) <= unique_thresh:
                feat_type = "categorical"

            # Sometimes a variable has only integer values but definitely isn't categorical
            else:
                feat_type = "real"

        # Real-valued
        else:
            if all(feat_values > 0):
                feat_type = "positive_real"

            else:
                feat_type = "real"

        feat_types.append((feat_type, np.min(feat_values), np.max(feat_values)))

    return feat_types
