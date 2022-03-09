from contextlib import contextmanager
import os
import tempfile
import yaml
import warnings

import numpy as np

import mlflow
import types
import mlflow.utils.autologging_utils
from mlflow import pyfunc
from mlflow.exceptions import MlflowException
from mlflow.utils.annotations import experimental
from mlflow.utils.uri import append_to_uri_path
from mlflow.models import Model

from mlflow.tracking.artifact_utils import _download_artifact_from_uri
from mlflow.models.model import MLMODEL_FILE_NAME
from mlflow.models.signature import ModelSignature
from mlflow.models.utils import ModelInputExample, _save_example
from mlflow.utils.environment import (
    _mlflow_conda_env,
    _get_pip_deps,
    _validate_env_arguments,
    _process_pip_requirements,
    _process_conda_env,
    _CONSTRAINTS_FILE_NAME,
    _CONDA_ENV_FILE_NAME,
    _REQUIREMENTS_FILE_NAME,
)
from mlflow.utils.requirements_utils import _get_package_name
from mlflow.utils.file_utils import write_to
from mlflow.utils.docstring_utils import format_docstring, LOG_MODEL_PARAM_DOCS
from mlflow.utils.model_utils import (
    _get_flavor_configuration,
    _validate_and_copy_code_paths,
    _add_code_from_conf_to_system_path,
)
from mlflow.protos.databricks_pb2 import RESOURCE_ALREADY_EXISTS
from mlflow.tracking._model_registry import DEFAULT_AWAIT_MAX_SLEEP_SECONDS

FLAVOR_NAME = "shap"

_MAXIMUM_BACKGROUND_DATA_SIZE = 100
_DEFAULT_ARTIFACT_PATH = "model_explanations_shap"
_SUMMARY_BAR_PLOT_FILE_NAME = "summary_bar_plot.png"
_BASE_VALUES_FILE_NAME = "base_values.npy"
_SHAP_VALUES_FILE_NAME = "shap_values.npy"
_UNKNOWN_MODEL_FLAVOR = "unknown"
_UNDERLYING_MODEL_SUBPATH = "underlying_model"


def get_underlying_model_flavor(model):
    """
    Find the underlying models flavor.

    :param model: underlying model of the explainer.
    """

    # checking if underlying model is wrapped

    if hasattr(model, "inner_model"):
        unwrapped_model = model.inner_model

        # check if passed model is a method of object
        if isinstance(unwrapped_model, types.MethodType):
            model_object = unwrapped_model.__self__

            # check if model object is of type sklearn
            try:
                import sklearn

                if issubclass(type(model_object), sklearn.base.BaseEstimator):
                    return mlflow.sklearn.FLAVOR_NAME
            except ImportError:
                pass

        # check if passed model is of type pytorch
        try:
            import torch

            if issubclass(type(unwrapped_model), torch.nn.Module):
                return mlflow.pytorch.FLAVOR_NAME
        except ImportError:
            pass

    return _UNKNOWN_MODEL_FLAVOR


def get_default_pip_requirements():
    """
    :return: A list of default pip requirements for MLflow Models produced by this flavor.
             Calls to :func:`save_explainer()` and :func:`log_explainer()` produce a pip environment
             that, at minimum, contains these requirements.
    """
    import shap

    return ["shap=={}".format(shap.__version__)]


def get_default_conda_env():
    """
    :return: The default Conda environment for
             MLflow Models produced by calls to
             :func:`save_explainer()` and :func:`log_explainer()`.
    """
    return _mlflow_conda_env(additional_pip_deps=get_default_pip_requirements())


def _load_pyfunc(path):
    """
    Load PyFunc implementation. Called by ``pyfunc.load_pyfunc``.
    """
    return _SHAPWrapper(path)


@contextmanager
def _log_artifact_contextmanager(out_file, artifact_path=None):
    """
    A context manager to make it easier to log an artifact.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = os.path.join(tmp_dir, out_file)
        yield tmp_path
        mlflow.log_artifact(tmp_path, artifact_path)


def _log_numpy(numpy_obj, out_file, artifact_path=None):
    """
    Log a numpy object.
    """
    with _log_artifact_contextmanager(out_file, artifact_path) as tmp_path:
        np.save(tmp_path, numpy_obj)


def _log_matplotlib_figure(fig, out_file, artifact_path=None):
    """
    Log a matplotlib figure.
    """
    with _log_artifact_contextmanager(out_file, artifact_path) as tmp_path:
        fig.savefig(tmp_path)


def _get_conda_env_for_underlying_model(underlying_model_path):
    underlying_model_conda_path = os.path.join(underlying_model_path, "conda.yaml")
    with open(underlying_model_conda_path, "r") as underlying_model_conda_file:
        return yaml.safe_load(underlying_model_conda_file)


@experimental
def log_explanation(predict_function, features, artifact_path=None):
    r"""
    Given a ``predict_function`` capable of computing ML model output on the provided ``features``,
    computes and logs explanations of an ML model's output. Explanations are logged as a directory
    of artifacts containing the following items generated by `SHAP`_ (SHapley Additive
    exPlanations).

        - Base values
        - SHAP values (computed using `shap.KernelExplainer`_)
        - Summary bar plot (shows the average impact of each feature on model output)

    :param predict_function:
        A function to compute the output of a model (e.g. ``predict_proba`` method of
        scikit-learn classifiers). Must have the following signature:

        .. code-block:: python

            def predict_function(X) -> pred:
                ...

        - ``X``: An array-like object whose shape should be (# samples, # features).
        - ``pred``: An array-like object whose shape should be (# samples) for
          a regressor or (# classes, # samples) for a classifier. For a classifier,
          the values in ``pred`` should correspond to the predicted probability of each class.

        Acceptable array-like object types:

            - ``numpy.array``
            - ``pandas.DataFrame``
            - ``shap.common.DenseData``
            - ``scipy.sparse matrix``

    :param features:
        A matrix of features to compute SHAP values with. The provided features should
        have shape (# samples, # features), and can be either of the array-like object
        types listed above.

        .. note::
            Background data for `shap.KernelExplainer`_ is generated by subsampling ``features``
            with `shap.kmeans`_. The background data size is limited to 100 rows for performance
            reasons.

    :param artifact_path:
        The run-relative artifact path to which the explanation is saved.
        If unspecified, defaults to "model_explanations_shap".

    :return: Artifact URI of the logged explanations.

    .. _SHAP: https://github.com/slundberg/shap

    .. _shap.KernelExplainer: https://shap.readthedocs.io/en/latest/generated
        /shap.KernelExplainer.html#shap.KernelExplainer

    .. _shap.kmeans: https://github.com/slundberg/shap/blob/v0.36.0/shap/utils/_legacy.py#L9

    .. code-block:: python
        :caption: Example

        import os

        import numpy as np
        import pandas as pd
        from sklearn.datasets import load_diabetes
        from sklearn.linear_model import LinearRegression

        import mlflow

        # prepare training data
        X, y = dataset = load_diabetes(return_X_y=True, as_frame=True)
        X = pd.DataFrame(dataset.data[:50, :8], columns=dataset.feature_names[:8])
        y = dataset.target[:50]

        # train a model
        model = LinearRegression()
        model.fit(X, y)

        # log an explanation
        with mlflow.start_run() as run:
            mlflow.shap.log_explanation(model.predict, X)

        # list artifacts
        client = mlflow.tracking.MlflowClient()
        artifact_path = "model_explanations_shap"
        artifacts = [x.path for x in client.list_artifacts(run.info.run_id, artifact_path)]
        print("# artifacts:")
        print(artifacts)

        # load back the logged explanation
        dst_path = client.download_artifacts(run.info.run_id, artifact_path)
        base_values = np.load(os.path.join(dst_path, "base_values.npy"))
        shap_values = np.load(os.path.join(dst_path, "shap_values.npy"))

        print("\n# base_values:")
        print(base_values)
        print("\n# shap_values:")
        print(shap_values[:3])

    .. code-block:: text
        :caption: Output

        # artifacts:
        ['model_explanations_shap/base_values.npy',
         'model_explanations_shap/shap_values.npy',
         'model_explanations_shap/summary_bar_plot.png']

        # base_values:
        20.502000000000002

        # shap_values:
        [[ 2.09975523  0.4746513   7.63759026  0.        ]
         [ 2.00883109 -0.18816665 -0.14419184  0.        ]
         [ 2.00891772 -0.18816665 -0.14419184  0.        ]]

    .. figure:: ../_static/images/shap-ui-screenshot.png

        Logged artifacts
    """
    import matplotlib.pyplot as plt
    import shap

    artifact_path = _DEFAULT_ARTIFACT_PATH if artifact_path is None else artifact_path
    with mlflow.utils.autologging_utils.disable_autologging():
        background_data = shap.kmeans(features, min(_MAXIMUM_BACKGROUND_DATA_SIZE, len(features)))
        explainer = shap.KernelExplainer(predict_function, background_data)
        shap_values = explainer.shap_values(features)

        _log_numpy(explainer.expected_value, _BASE_VALUES_FILE_NAME, artifact_path)
        _log_numpy(shap_values, _SHAP_VALUES_FILE_NAME, artifact_path)

        shap.summary_plot(shap_values, features, plot_type="bar", show=False)
        fig = plt.gcf()
        fig.tight_layout()
        _log_matplotlib_figure(fig, _SUMMARY_BAR_PLOT_FILE_NAME, artifact_path)
        plt.close(fig)

    return append_to_uri_path(mlflow.active_run().info.artifact_uri, artifact_path)


@experimental
@format_docstring(LOG_MODEL_PARAM_DOCS.format(package_name=FLAVOR_NAME))
def log_explainer(
    explainer,
    artifact_path,
    serialize_model_using_mlflow=True,
    conda_env=None,
    code_paths=None,
    registered_model_name=None,
    signature: ModelSignature = None,
    input_example: ModelInputExample = None,
    await_registration_for=DEFAULT_AWAIT_MAX_SLEEP_SECONDS,
    pip_requirements=None,
    extra_pip_requirements=None,
):
    """
    Log an SHAP explainer as an MLflow artifact for the current run.

    :param explainer: SHAP explainer to be saved.
    :param artifact_path: Run-relative artifact path.
    :param serialize_model_using_mlflow: When set to True, MLflow will extract the underlying
                                        model and serialize it as an MLmodel, otherwise it
                                        uses SHAP's internal serialization. Defaults to True.
                                        Currently MLflow serialization is only supported for
                                        models of 'sklearn' or 'pytorch' flavors.

    :param conda_env: {{ conda_env }}
    :param code_paths: A list of local filesystem paths to Python file dependencies (or directories
                       containing file dependencies). These files are *prepended* to the system
                       path when the model is loaded.
    :param registered_model_name: If given, create a model version under
                                  ``registered_model_name``, also creating a registered model if one
                                  with the given name does not exist.

    :param signature: :py:class:`ModelSignature <mlflow.models.ModelSignature>`
                      describes model input and output :py:class:`Schema <mlflow.types.Schema>`.
                      The model signature can be :py:func:`inferred <mlflow.models.infer_signature>`
                      from datasets with valid model input (e.g. the training dataset with target
                      column omitted) and valid model output (e.g. model predictions generated on
                      the training dataset), for example:

                      .. code-block:: python

                        from mlflow.models.signature import infer_signature
                        train = df.drop_column("target_label")
                        predictions = ... # compute model predictions
                        signature = infer_signature(train, predictions)
    :param input_example: Input example provides one or several instances of valid
                          model input. The example can be used as a hint of what data to feed the
                          model. The given example will be converted to a Pandas DataFrame and then
                          serialized to json using the Pandas split-oriented format. Bytes are
                          base64-encoded.
    :param await_registration_for: Number of seconds to wait for the model version to finish
                            being created and is in ``READY`` status. By default, the function
                            waits for five minutes. Specify 0 or None to skip waiting.
    :param pip_requirements: {{ pip_requirements }}
    :param extra_pip_requirements: {{ extra_pip_requirements }}
    """

    Model.log(
        artifact_path=artifact_path,
        flavor=mlflow.shap,
        explainer=explainer,
        conda_env=conda_env,
        code_paths=code_paths,
        serialize_model_using_mlflow=serialize_model_using_mlflow,
        registered_model_name=registered_model_name,
        signature=signature,
        input_example=input_example,
        await_registration_for=await_registration_for,
        pip_requirements=pip_requirements,
        extra_pip_requirements=extra_pip_requirements,
    )


@experimental
@format_docstring(LOG_MODEL_PARAM_DOCS.format(package_name=FLAVOR_NAME))
def save_explainer(
    explainer,
    path,
    serialize_model_using_mlflow=True,
    conda_env=None,
    code_paths=None,
    mlflow_model=None,
    signature: ModelSignature = None,
    input_example: ModelInputExample = None,
    pip_requirements=None,
    extra_pip_requirements=None,
):
    """
    Save a SHAP explainer to a path on the local file system. Produces an MLflow Model
    containing the following flavors:

        - :py:mod:`mlflow.shap`
        - :py:mod:`mlflow.pyfunc`

    :param explainer: SHAP explainer to be saved.
    :param path: Local path where the explainer is to be saved.
    :param serialize_model_using_mlflow: When set to True, MLflow will extract the underlying
                                         model and serialize it as an MLmodel, otherwise it
                                         uses SHAP's internal serialization. Defaults to True.
                                         Currently MLflow serialization is only supported for
                                         models of 'sklearn' or 'pytorch' flavors.

    :param conda_env: {{ conda_env }}
    :param code_paths: A list of local filesystem paths to Python file dependencies (or directories
                       containing file dependencies). These files are *prepended* to the system
                       path when the model is loaded.
    :param mlflow_model: :py:mod:`mlflow.models.Model` this flavor is being added to.
    :param signature: :py:class:`ModelSignature <mlflow.models.ModelSignature>`
                      describes model input and output :py:class:`Schema <mlflow.types.Schema>`.
                      The model signature can be :py:func:`inferred <mlflow.models.infer_signature>`
                      from datasets with valid model input (e.g. the training dataset with target
                      column omitted) and valid model output (e.g. model predictions generated on
                      the training dataset), for example:

                      .. code-block:: python

                        from mlflow.models.signature import infer_signature
                        train = df.drop_column("target_label")
                        predictions = ... # compute model predictions
                        signature = infer_signature(train, predictions)
    :param input_example: Input example provides one or several instances of valid
                          model input. The example can be used as a hint of what data to feed the
                          model. The given example will be converted to a Pandas DataFrame and then
                          serialized to json using the Pandas split-oriented format. Bytes are
                          base64-encoded.
    :param pip_requirements: {{ pip_requirements }}
    :param extra_pip_requirements: {{ extra_pip_requirements }}
    """
    import shap

    _validate_env_arguments(conda_env, pip_requirements, extra_pip_requirements)

    if os.path.exists(path):
        raise MlflowException(
            message="Path '{}' already exists".format(path),
            error_code=RESOURCE_ALREADY_EXISTS,
        )

    os.makedirs(path)
    code_dir_subpath = _validate_and_copy_code_paths(code_paths, path)

    if mlflow_model is None:
        mlflow_model = Model()
    if signature is not None:
        mlflow_model.signature = signature
    if input_example is not None:
        _save_example(mlflow_model, input_example, path)

    underlying_model_flavor = None
    underlying_model_path = None
    serializable_by_mlflow = False

    # saving the underlying model if required
    if serialize_model_using_mlflow:
        underlying_model_flavor = get_underlying_model_flavor(explainer.model)

        if underlying_model_flavor != _UNKNOWN_MODEL_FLAVOR:
            serializable_by_mlflow = True  # prevents SHAP from serializing the underlying model
            underlying_model_path = os.path.join(path, _UNDERLYING_MODEL_SUBPATH)
        else:
            warnings.warn(
                "Unable to serialize underlying model using MLflow, will use SHAP serialization"
            )

        if underlying_model_flavor == mlflow.sklearn.FLAVOR_NAME:
            mlflow.sklearn.save_model(explainer.model.inner_model.__self__, underlying_model_path)
        elif underlying_model_flavor == mlflow.pytorch.FLAVOR_NAME:
            mlflow.pytorch.save_model(explainer.model.inner_model, underlying_model_path)

    # saving the explainer object
    explainer_data_subpath = "explainer.shap"
    explainer_output_path = os.path.join(path, explainer_data_subpath)
    with open(explainer_output_path, "wb") as explainer_output_file_handle:
        if serialize_model_using_mlflow and serializable_by_mlflow:
            explainer.save(explainer_output_file_handle, model_saver=False)
        else:
            explainer.save(explainer_output_file_handle)

    pyfunc.add_to_model(
        mlflow_model,
        loader_module="mlflow.shap",
        model_path=explainer_data_subpath,
        underlying_model_flavor=underlying_model_flavor,
        env=_CONDA_ENV_FILE_NAME,
        code=code_dir_subpath,
    )

    mlflow_model.add_flavor(
        FLAVOR_NAME,
        shap_version=shap.__version__,
        serialized_explainer=explainer_data_subpath,
        underlying_model_flavor=underlying_model_flavor,
        code=code_dir_subpath,
    )

    mlflow_model.save(os.path.join(path, MLMODEL_FILE_NAME))

    if conda_env is None:
        if pip_requirements is None:
            default_reqs = get_default_pip_requirements()
            # To ensure `_load_pyfunc` can successfully load the model during the dependency
            # inference, `mlflow_model.save` must be called beforehand to save an MLmodel file.
            inferred_reqs = mlflow.models.infer_pip_requirements(
                path,
                FLAVOR_NAME,
                fallback=default_reqs,
            )
            default_reqs = sorted(set(inferred_reqs).union(default_reqs))
        else:
            default_reqs = None
        conda_env, pip_requirements, pip_constraints = _process_pip_requirements(
            default_reqs,
            pip_requirements,
            extra_pip_requirements,
        )
    else:
        conda_env, pip_requirements, pip_constraints = _process_conda_env(conda_env)

    if underlying_model_path is not None:
        underlying_model_conda_env = _get_conda_env_for_underlying_model(underlying_model_path)
        conda_env = _merge_environments(conda_env, underlying_model_conda_env)
        pip_requirements = _get_pip_deps(conda_env)

    with open(os.path.join(path, _CONDA_ENV_FILE_NAME), "w") as f:
        yaml.safe_dump(conda_env, stream=f, default_flow_style=False)

    # Save `constraints.txt` if necessary
    if pip_constraints:
        write_to(os.path.join(path, _CONSTRAINTS_FILE_NAME), "\n".join(pip_constraints))

    # Save `requirements.txt`
    write_to(os.path.join(path, _REQUIREMENTS_FILE_NAME), "\n".join(pip_requirements))


# Defining save_model (Required by Model.log) to refer to save_explainer
save_model = save_explainer


def _get_conda_and_pip_dependencies(conda_env):
    """
    Extract conda and pip dependencies from conda environments

    :param conda_env: Conda environment
    """

    conda_deps = []
    pip_deps = []

    for dependency in conda_env["dependencies"]:
        if isinstance(dependency, dict) and dependency["pip"]:
            for pip_dependency in dependency["pip"]:
                if pip_dependency != "mlflow":
                    pip_deps.append(pip_dependency)
        else:
            package_name = _get_package_name(dependency)
            if package_name is not None and package_name not in ["python", "pip"]:
                conda_deps.append(dependency)

    return conda_deps, pip_deps


def _union_lists(l1, l2):
    """
    Returns the union of two lists as a new list.
    """
    return l1 + [x for x in l2 if x not in l1]


def _merge_environments(shap_environment, model_environment):
    """
    Merge conda environments of underlying model and shap.

    :param shap_environment: SHAP conda environment.
    :param model_environment: Underlying model conda environment.
    """
    # merge the channels from the two environments and remove the default conda
    # channels if present since its added later in `_mlflow_conda_env`
    merged_conda_channels = _union_lists(
        shap_environment["channels"], model_environment["channels"]
    )
    merged_conda_channels = [x for x in merged_conda_channels if x != "conda-forge"]

    shap_conda_deps, shap_pip_deps = _get_conda_and_pip_dependencies(shap_environment)
    model_conda_deps, model_pip_deps = _get_conda_and_pip_dependencies(model_environment)

    merged_conda_deps = _union_lists(shap_conda_deps, model_conda_deps)
    merged_pip_deps = _union_lists(shap_pip_deps, model_pip_deps)
    return _mlflow_conda_env(
        additional_conda_deps=merged_conda_deps,
        additional_pip_deps=merged_pip_deps,
        additional_conda_channels=merged_conda_channels,
    )


@experimental
def load_explainer(model_uri):
    """
    Load a SHAP explainer from a local file or a run.

    :param model_uri: The location, in URI format, of the MLflow model, for example:

                      - ``/Users/me/path/to/local/model``
                      - ``relative/path/to/local/model``
                      - ``s3://my_bucket/path/to/model``
                      - ``runs:/<mlflow_run_id>/run-relative/path/to/model``
                      - ``models:/<model_name>/<model_version>``
                      - ``models:/<model_name>/<stage>``

                      For more information about supported URI schemes, see
                      `Referencing Artifacts <https://www.mlflow.org/docs/latest/concepts.html#
                      artifact-locations>`_.

    :return: A SHAP explainer.
    """

    explainer_path = _download_artifact_from_uri(artifact_uri=model_uri)
    flavor_conf = _get_flavor_configuration(model_path=explainer_path, flavor_name=FLAVOR_NAME)
    _add_code_from_conf_to_system_path(explainer_path, flavor_conf)
    explainer_artifacts_path = os.path.join(explainer_path, flavor_conf["serialized_explainer"])
    underlying_model_flavor = flavor_conf["underlying_model_flavor"]
    model = None

    if underlying_model_flavor != _UNKNOWN_MODEL_FLAVOR:
        underlying_model_path = os.path.join(explainer_path, _UNDERLYING_MODEL_SUBPATH)
        if underlying_model_flavor == mlflow.sklearn.FLAVOR_NAME:
            model = mlflow.sklearn._load_pyfunc(underlying_model_path).predict
        elif underlying_model_flavor == mlflow.pytorch.FLAVOR_NAME:
            model = mlflow.pytorch._load_model(os.path.join(underlying_model_path, "data"))

    return _load_explainer(explainer_file=explainer_artifacts_path, model=model)


@experimental
def _load_explainer(explainer_file, model=None):
    """
    Load a SHAP explainer saved as an MLflow artifact on the local file system.

    :param explainer_file: Local filesystem path to the MLflow Model saved with the ``shap`` flavor
    :param model: model to override underlying explainer model.
    """
    import shap

    def inject_model_loader(_in_file):
        return model

    with open(explainer_file, "rb") as explainer:
        if model is None:
            explainer = shap.Explainer.load(explainer)
        else:
            explainer = shap.Explainer.load(explainer, model_loader=inject_model_loader)
        return explainer


class _SHAPWrapper:
    def __init__(self, path):
        flavor_conf = _get_flavor_configuration(model_path=path, flavor_name=FLAVOR_NAME)
        shap_explainer_artifacts_path = os.path.join(path, flavor_conf["serialized_explainer"])
        underlying_model_flavor = flavor_conf["underlying_model_flavor"]
        model = None
        if underlying_model_flavor != _UNKNOWN_MODEL_FLAVOR:
            underlying_model_path = os.path.join(path, _UNDERLYING_MODEL_SUBPATH)
            if underlying_model_flavor == mlflow.sklearn.FLAVOR_NAME:
                model = mlflow.sklearn._load_pyfunc(underlying_model_path).predict
            elif underlying_model_flavor == mlflow.pytorch.FLAVOR_NAME:
                model = mlflow.pytorch._load_model(os.path.join(underlying_model_path, "data"))

        self.explainer = _load_explainer(explainer_file=shap_explainer_artifacts_path, model=model)

    def predict(self, dataframe):
        return self.explainer(dataframe.values).values
