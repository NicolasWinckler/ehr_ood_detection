"""
Define how models for experiments are initialized.
"""

# STD
from typing import Optional, Iterable, Dict, Tuple, List

# EXT
from sklearn.decomposition import PCA
from sklearn.svm import OneClassSVM

# PROJECT
from uncertainty_estimation.models.novelty_estimator import NoveltyEstimator
from uncertainty_estimation.models.nn_ensemble import NNEnsemble, AnchoredNNEnsemble
from uncertainty_estimation.models.autoencoder import AE
from uncertainty_estimation.models.mlp import MLP, BayesianMLP
from uncertainty_estimation.models.info import (
    AVAILABLE_MODELS,
    AVAILABLE_SCORING_FUNCS,
    NEURAL_MODELS,
    TRAIN_PARAMS,
    MODEL_PARAMS,
)
from uncertainty_estimation.utils.types import ModelInfo


def init_models(
    input_dim: int,
    selection: Iterable[str] = AVAILABLE_MODELS,
    scoring_funcs: Dict[str, Tuple[Optional[str], ...]] = AVAILABLE_SCORING_FUNCS,
) -> List[ModelInfo]:
    """
    Initialize the models for the experiments.

    Parameters
    ----------
    input_dim: int
        Dimensionality of input for models.
    selection: Iterable[str]
        Iterable of strings defining the names of all the models that will be initialized. Default is the set
        AVAILABLE_MODELS defined in uncertainty_estimation.models.infos.
    scoring_funcs: Dict[str, Tuple[Optional[str], ...]]
        Specify the scoring functions by name by model which are being used to determine the novelty of a sample.

    Returns
    -------
    List[ModelInfo]
        A list of tuples containing the model, the names of metrics used for uncertainty estimation as a tuple as well
        as the model name.
    """
    selection = set(selection)
    assert len(AVAILABLE_MODELS.intersection(selection)) == len(
        selection
    ), f"Unknown models specified: {', '.join([name for name in selection if name not in AVAILABLE_MODELS])}"

    model_classes = {
        "PPCA": PCA,
        "SVM": OneClassSVM,
        "AE": AE,
        "NN": MLP,
        "BNN": BayesianMLP,
        "MCDropout": MLP,
        "NNEnsemble": NNEnsemble,
        "BootstrappedNNEnsemble": NNEnsemble,
        "AnchoredNNEnsemble": AnchoredNNEnsemble,
    }

    return [
        (
            NoveltyEstimator(
                model_classes[model_name],
                MODEL_PARAMS[model_name]
                # Add input_dim for neural network-based models
                if model_name not in NEURAL_MODELS
                else {"input_dim": input_dim, **MODEL_PARAMS[model_name]},
                TRAIN_PARAMS[model_name],
                model_name,
            ),
            scoring_funcs[model_name],
            model_name,
        )
        for model_name in selection
    ]