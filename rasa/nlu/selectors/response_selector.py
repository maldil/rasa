import logging

import numpy as np
import tensorflow as tf
from pathlib import Path

from typing import Any, Dict, Optional, Text, Tuple, Union, List, Type

import rasa.utils.io as io_utils
from rasa.nlu.config import InvalidConfigError
from rasa.nlu.training_data import TrainingData, Message
from rasa.nlu.components import Component
from rasa.nlu.featurizers.featurizer import Featurizer
from rasa.nlu.model import Metadata
from rasa.nlu.classifiers.diet_classifier import (
    DIETClassifier,
    DIET,
    LABEL_IDS,
    EntityTagSpec,
    TEXT_SEQUENCE_LENGTH,
    LABEL_SEQUENCE_LENGTH,
    TEXT_SEQUENCE_FEATURES,
    LABEL_SEQUENCE_FEATURES,
    TEXT_SENTENCE_FEATURES,
    LABEL_SENTENCE_FEATURES,
)
from rasa.utils.tensorflow.constants import (
    LABEL,
    HIDDEN_LAYERS_SIZES,
    SHARE_HIDDEN_LAYERS,
    TRANSFORMER_SIZE,
    NUM_TRANSFORMER_LAYERS,
    NUM_HEADS,
    BATCH_SIZES,
    BATCH_STRATEGY,
    EPOCHS,
    RANDOM_SEED,
    LEARNING_RATE,
    DENSE_DIMENSION,
    RANKING_LENGTH,
    LOSS_TYPE,
    SIMILARITY_TYPE,
    NUM_NEG,
    SPARSE_INPUT_DROPOUT,
    DENSE_INPUT_DROPOUT,
    MASKED_LM,
    ENTITY_RECOGNITION,
    INTENT_CLASSIFICATION,
    EVAL_NUM_EXAMPLES,
    EVAL_NUM_EPOCHS,
    UNIDIRECTIONAL_ENCODER,
    DROP_RATE,
    DROP_RATE_ATTENTION,
    WEIGHT_SPARSITY,
    NEGATIVE_MARGIN_SCALE,
    REGULARIZATION_CONSTANT,
    SCALE_LOSS,
    USE_MAX_NEG_SIM,
    MAX_NEG_SIM,
    MAX_POS_SIM,
    EMBEDDING_DIMENSION,
    BILOU_FLAG,
    KEY_RELATIVE_ATTENTION,
    VALUE_RELATIVE_ATTENTION,
    MAX_RELATIVE_POSITION,
    RETRIEVAL_INTENT,
    TRAIN_ON_TEXT,
    SOFTMAX,
    AUTO,
    BALANCED,
    TENSORBOARD_LOG_DIR,
    TENSORBOARD_LOG_LEVEL,
    CONCAT_DIMENSION,
    FEATURIZERS,
)
from rasa.nlu.constants import (
    RESPONSE,
    RESPONSE_SELECTOR_PROPERTY_NAME,
    RESPONSE_KEY,
    INTENT_RESPONSE_KEY,
    INTENT,
    DEFAULT_OPEN_UTTERANCE_TYPE,
    TEXT,
)

from rasa.utils.tensorflow.model_data import RasaModelData
from rasa.utils.tensorflow.models import RasaModel

logger = logging.getLogger(__name__)


class ResponseSelector(DIETClassifier):
    """Response selector using supervised embeddings.

    The response selector embeds user inputs
    and candidate response into the same space.
    Supervised embeddings are trained by maximizing similarity between them.
    It also provides rankings of the response that did not "win".

    The supervised response selector needs to be preceded by
    a featurizer in the pipeline.
    This featurizer creates the features used for the embeddings.
    It is recommended to use ``CountVectorsFeaturizer`` that
    can be optionally preceded by ``SpacyNLP`` and ``SpacyTokenizer``.

    Based on the starspace idea from: https://arxiv.org/abs/1709.03856.
    However, in this implementation the `mu` parameter is treated differently
    and additional hidden layers are added together with dropout.
    """

    @classmethod
    def required_components(cls) -> List[Type[Component]]:
        return [Featurizer]

    defaults = {
        # ## Architecture of the used neural network
        # Hidden layer sizes for layers before the embedding layers for user message
        # and labels.
        # The number of hidden layers is equal to the length of the corresponding
        # list.
        HIDDEN_LAYERS_SIZES: {TEXT: [256, 128], LABEL: [256, 128]},
        # Whether to share the hidden layer weights between input words and responses
        SHARE_HIDDEN_LAYERS: False,
        # Number of units in transformer
        TRANSFORMER_SIZE: None,
        # Number of transformer layers
        NUM_TRANSFORMER_LAYERS: 0,
        # Number of attention heads in transformer
        NUM_HEADS: 4,
        # If 'True' use key relative embeddings in attention
        KEY_RELATIVE_ATTENTION: False,
        # If 'True' use key relative embeddings in attention
        VALUE_RELATIVE_ATTENTION: False,
        # Max position for relative embeddings
        MAX_RELATIVE_POSITION: None,
        # Use a unidirectional or bidirectional encoder.
        UNIDIRECTIONAL_ENCODER: False,
        # ## Training parameters
        # Initial and final batch sizes:
        # Batch size will be linearly increased for each epoch.
        BATCH_SIZES: [64, 256],
        # Strategy used when creating batches.
        # Can be either 'sequence' or 'balanced'.
        BATCH_STRATEGY: BALANCED,
        # Number of epochs to train
        EPOCHS: 300,
        # Set random seed to any 'int' to get reproducible results
        RANDOM_SEED: None,
        # Initial learning rate for the optimizer
        LEARNING_RATE: 0.001,
        # ## Parameters for embeddings
        # Dimension size of embedding vectors
        EMBEDDING_DIMENSION: 20,
        # Default dense dimension to use if no dense features are present.
        DENSE_DIMENSION: {TEXT: 512, LABEL: 512},
        # Default dimension to use for concatenating sequence and sentence features.
        CONCAT_DIMENSION: {TEXT: 512, LABEL: 512},
        # The number of incorrect labels. The algorithm will minimize
        # their similarity to the user input during training.
        NUM_NEG: 20,
        # Type of similarity measure to use, either 'auto' or 'cosine' or 'inner'.
        SIMILARITY_TYPE: AUTO,
        # The type of the loss function, either 'softmax' or 'margin'.
        LOSS_TYPE: SOFTMAX,
        # Number of top actions to normalize scores for loss type 'softmax'.
        # Set to 0 to turn off normalization.
        RANKING_LENGTH: 10,
        # Indicates how similar the algorithm should try to make embedding vectors
        # for correct labels.
        # Should be 0.0 < ... < 1.0 for 'cosine' similarity type.
        MAX_POS_SIM: 0.8,
        # Maximum negative similarity for incorrect labels.
        # Should be -1.0 < ... < 1.0 for 'cosine' similarity type.
        MAX_NEG_SIM: -0.4,
        # If 'True' the algorithm only minimizes maximum similarity over
        # incorrect intent labels, used only if 'loss_type' is set to 'margin'.
        USE_MAX_NEG_SIM: True,
        # Scale loss inverse proportionally to confidence of correct prediction
        SCALE_LOSS: True,
        # ## Regularization parameters
        # The scale of regularization
        REGULARIZATION_CONSTANT: 0.002,
        # Sparsity of the weights in dense layers
        WEIGHT_SPARSITY: 0.0,
        # The scale of how important is to minimize the maximum similarity
        # between embeddings of different labels.
        NEGATIVE_MARGIN_SCALE: 0.8,
        # Dropout rate for encoder
        DROP_RATE: 0.2,
        # Dropout rate for attention
        DROP_RATE_ATTENTION: 0,
        # If 'True' apply dropout to sparse input tensors
        SPARSE_INPUT_DROPOUT: False,
        # If 'True' apply dropout to dense input tensors
        DENSE_INPUT_DROPOUT: False,
        # ## Evaluation parameters
        # How often calculate validation accuracy.
        # Small values may hurt performance, e.g. model accuracy.
        EVAL_NUM_EPOCHS: 20,
        # How many examples to use for hold out validation set
        # Large values may hurt performance, e.g. model accuracy.
        EVAL_NUM_EXAMPLES: 0,
        # ## Selector config
        # If 'True' random tokens of the input message will be masked and the model
        # should predict those tokens.
        MASKED_LM: False,
        # Name of the intent for which this response selector is to be trained
        INTENT_RESPONSE_KEY: None,
        # Boolean flag to check if actual text of the response should be used as ground truth label for
        # training the model.
        TRAIN_ON_TEXT: False,
        # If you want to use tensorboard to visualize training and validation metrics,
        # set this option to a valid output directory.
        TENSORBOARD_LOG_DIR: None,
        # Define when training metrics for tensorboard should be logged.
        # Either after every epoch or for every training step.
        # Valid values: 'epoch' and 'minibatch'
        TENSORBOARD_LOG_LEVEL: "epoch",
        # Specify what features to use as sequence and sentence features
        # By default all features in the pipeline are used.
        FEATURIZERS: [],
    }

    def __init__(
        self,
        component_config: Optional[Dict[Text, Any]] = None,
        index_label_id_mapping: Optional[Dict[int, Text]] = None,
        entity_tag_specs: Optional[List[EntityTagSpec]] = None,
        model: Optional[RasaModel] = None,
        retrieval_intent_mapping: Optional[Dict[Text, Text]] = None,
        responses: Optional[Dict[Text, List[Dict[Text, Any]]]] = None,
    ) -> None:

        component_config = component_config or {}

        # the following properties cannot be adapted for the ResponseSelector
        component_config[INTENT_CLASSIFICATION] = True
        component_config[ENTITY_RECOGNITION] = False
        component_config[BILOU_FLAG] = None
        self.retrieval_intent_mapping = retrieval_intent_mapping or {}
        self.responses = responses or {}

        super().__init__(
            component_config, index_label_id_mapping, entity_tag_specs, model
        )

    @property
    def label_key(self) -> Text:
        return LABEL_IDS

    @staticmethod
    def model_class() -> Type[RasaModel]:
        return DIET2DIET

    def _load_selector_params(self, config: Dict[Text, Any]) -> None:
        self.retrieval_intent = config[INTENT_RESPONSE_KEY]

    def _check_config_parameters(self) -> None:
        super()._check_config_parameters()
        self._load_selector_params(self.component_config)

    @staticmethod
    def _create_retrieval_intent_mapping(
        training_data: TrainingData,
    ) -> Dict[Text, Text]:
        """Create response_key dictionary"""

        retrieval_intent_mapping = {}
        for example in training_data.intent_examples:
            retrieval_intent_mapping[example.get(RESPONSE)] = example.get(
                INTENT_RESPONSE_KEY
            )

        return retrieval_intent_mapping

    @staticmethod
    def _set_message_property(
        message: Message, prediction_dict: Dict[Text, Any], selector_key: Text
    ) -> None:
        message_selector_properties = message.get(RESPONSE_SELECTOR_PROPERTY_NAME, {})
        message_selector_properties[selector_key] = prediction_dict
        message.set(
            RESPONSE_SELECTOR_PROPERTY_NAME,
            message_selector_properties,
            add_to_output=True,
        )

    def preprocess_train_data(self, training_data: TrainingData) -> RasaModelData:
        """Prepares data for training.

        Performs sanity checks on training data, extracts encodings for labels.
        """

        if self.retrieval_intent:
            training_data = training_data.filter_training_examples(
                lambda ex: self.retrieval_intent == ex.get(INTENT)
            )
        else:
            # retrieval intent was left to its default value
            logger.info(
                "Retrieval intent parameter was left to its default value. This "
                "response selector will be trained on training examples combining "
                "all retrieval intents."
            )

        label_attribute = (
            RESPONSE if self.component_config[TRAIN_ON_TEXT] else INTENT_RESPONSE_KEY
        )

        label_id_index_mapping = self._label_id_index_mapping(
            training_data, attribute=label_attribute
        )

        # Todo: Revisit, this might not be needed/could be simplified.
        self.retrieval_intent_mapping = self._create_retrieval_intent_mapping(
            training_data
        )

        self.responses = training_data.responses

        if not label_id_index_mapping:
            # no labels are present to train
            return RasaModelData()

        self.index_label_id_mapping = self._invert_mapping(label_id_index_mapping)

        self._label_data = self._create_label_data(
            training_data, label_id_index_mapping, attribute=label_attribute
        )

        model_data = self._create_model_data(
            training_data.intent_examples,
            label_id_index_mapping,
            label_attribute=label_attribute,
        )

        self._check_input_dimension_consistency(model_data)

        return model_data

    def _full_response(
        self, label: Dict[Text, Any]
    ) -> Optional[Tuple[Text, Dict[Text, Any]]]:
        """Given a label return the full response based on the labels id.

        Args:
            label: predicted label by the selector

        Returns:
            The match for the label that was found in the known responses. In
            contrast to the predicted label, the response doesn't only contain
            the text but also buttons, images, ...
        """
        for key, responses in self.responses.items():
            if self.component_config[TRAIN_ON_TEXT]:
                for response in responses:
                    if hash(response.get(TEXT, "")) == label.get("id"):
                        return key, response
            else:
                if hash(key) == label.get("id"):
                    # return the first response
                    return key, responses[0]
        return None

    def process(self, message: Message, **kwargs: Any) -> None:
        """Return the most likely response and its similarity to the input."""

        out = self._predict(message)
        label, label_ranking = self._predict_label(out)

        # label_key, label_response = self._full_response(label) or {TEXT: label.get("name")}
        label_retrieval_intent, label_responses = self._full_response(label)

        # retrieval_intent_name = (
        #     self.retrieval_intent_mapping.get(label.get("name"))
        #     if self.component_config[TRAIN_ON_TEXT]
        #     else label.get("name")
        # )

        for ranking in label_ranking:
            # ranking["full_retrieval_intent"] = (
            #     self.retrieval_intent_mapping.get(ranking.get("name"))
            #     if self.component_config[TRAIN_ON_TEXT]
            #     else ranking.get("name")
            # )
            ranking["full_retrieval_intent"] = (
                self.retrieval_intent_mapping.get(ranking.get("name"))
                if self.component_config[TRAIN_ON_TEXT]
                else ranking.get("name")
            )

        selector_key = (
            self.retrieval_intent
            if self.retrieval_intent
            else DEFAULT_OPEN_UTTERANCE_TYPE
        )

        logger.debug(
            f"Adding following selector key to message property: {selector_key}"
        )

        prediction_dict = {
            "response": label_responses,
            "ranking": label_ranking,
            "full_retrieval_intent": label_retrieval_intent,
        }

        self._set_message_property(message, prediction_dict, selector_key)

    def persist(self, file_name: Text, model_dir: Text) -> Dict[Text, Any]:
        """Persist this model into the passed directory.

        Return the metadata necessary to load the model again.
        """
        if self.model is None:
            return {"file": None}

        super().persist(file_name, model_dir)

        model_dir = Path(model_dir)

        io_utils.json_pickle(
            model_dir / f"{file_name}.retrieval_intent_mapping.pkl",
            self.retrieval_intent_mapping,
        )

        return {"file": file_name, "responses": self.responses}

    @classmethod
    def load(
        cls,
        meta: Dict[Text, Any],
        model_dir: Text = None,
        model_metadata: Metadata = None,
        cached_component: Optional["ResponseSelector"] = None,
        **kwargs: Any,
    ) -> "ResponseSelector":
        """Loads the trained model from the provided directory."""

        model = super().load(
            meta, model_dir, model_metadata, cached_component, **kwargs
        )
        if not meta.get("file"):
            return model  # pytype: disable=bad-return-type

        file_name = meta.get("file")
        model_dir = Path(model_dir)

        retrieval_intent_mapping = io_utils.json_unpickle(
            model_dir / f"{file_name}.retrieval_intent_mapping.pkl"
        )

        model.retrieval_intent_mapping = retrieval_intent_mapping
        model.responses = meta.get("responses", {})

        return model  # pytype: disable=bad-return-type


class DIET2DIET(DIET):
    def _check_data(self) -> None:
        if TEXT_SENTENCE_FEATURES not in self.data_signature:
            raise InvalidConfigError(
                f"No text features specified. "
                f"Cannot train '{self.__class__.__name__}' model."
            )
        if LABEL_SENTENCE_FEATURES not in self.data_signature:
            raise InvalidConfigError(
                f"No label features specified. "
                f"Cannot train '{self.__class__.__name__}' model."
            )
        if (
            self.config[SHARE_HIDDEN_LAYERS]
            and self.data_signature[TEXT_SENTENCE_FEATURES]
            != self.data_signature[LABEL_SENTENCE_FEATURES]
        ):
            raise ValueError(
                "If hidden layer weights are shared, data signatures "
                "for text_features and label_features must coincide."
            )

    def _create_metrics(self) -> None:
        # self.metrics preserve order
        # output losses first
        self.mask_loss = tf.keras.metrics.Mean(name="m_loss")
        self.response_loss = tf.keras.metrics.Mean(name="r_loss")
        # output accuracies second
        self.mask_acc = tf.keras.metrics.Mean(name="m_acc")
        self.response_acc = tf.keras.metrics.Mean(name="r_acc")

    def _update_metrics_to_log(self) -> None:
        debug_log_level = logging.getLogger("rasa").level == logging.DEBUG

        if self.config[MASKED_LM]:
            self.metrics_to_log.append("m_acc")
            if debug_log_level:
                self.metrics_to_log.append("m_loss")

        self.metrics_to_log.append("r_acc")
        if debug_log_level:
            self.metrics_to_log.append("r_loss")

        self._log_metric_info()

    def _log_metric_info(self) -> None:
        metric_name = {"t": "total", "m": "mask", "r": "response"}
        logger.debug("Following metrics will be logged during training: ")
        for metric in self.metrics_to_log:
            parts = metric.split("_")
            name = f"{metric_name[parts[0]]} {parts[1]}"
            logger.debug(f"  {metric} ({name})")

    def _prepare_layers(self) -> None:
        self.text_name = TEXT
        self.label_name = TEXT if self.config[SHARE_HIDDEN_LAYERS] else LABEL

        self._prepare_sequence_layers(self.text_name)
        self._prepare_sequence_layers(self.label_name)
        if self.config[MASKED_LM]:
            self._prepare_mask_lm_layers(self.text_name)
        self._prepare_label_classification_layers()

    def _create_all_labels(self) -> Tuple[tf.Tensor, tf.Tensor]:
        all_label_ids = self.tf_label_data[LABEL_IDS][0]

        sequence_mask_label = super()._get_mask_for(
            self.tf_label_data, LABEL_SEQUENCE_LENGTH
        )
        batch_dim = tf.shape(self.tf_label_data[LABEL_IDS][0])[0]
        sequence_lengths_label = self._get_sequence_lengths(
            self.tf_label_data, LABEL_SEQUENCE_LENGTH, batch_dim
        )
        mask_label = self._compute_mask(sequence_lengths_label)

        label_transformed, _, _, _ = self._create_sequence(
            self.tf_label_data[LABEL_SEQUENCE_FEATURES],
            self.tf_label_data[LABEL_SENTENCE_FEATURES],
            sequence_mask_label,
            mask_label,
            self.label_name,
        )
        sentence_label = self._last_token(label_transformed, sequence_lengths_label)

        all_labels_embed = self._tf_layers[f"embed.{LABEL}"](sentence_label)

        return all_label_ids, all_labels_embed

    def batch_loss(
        self, batch_in: Union[Tuple[tf.Tensor], Tuple[np.ndarray]]
    ) -> tf.Tensor:
        tf_batch_data = self.batch_to_model_data_format(batch_in, self.data_signature)

        batch_dim = self._get_batch_dim(tf_batch_data)
        sequence_mask_text = super()._get_mask_for(tf_batch_data, TEXT_SEQUENCE_LENGTH)
        sequence_lengths_text = self._get_sequence_lengths(
            tf_batch_data, TEXT_SEQUENCE_LENGTH, batch_dim
        )
        mask_text = self._compute_mask(sequence_lengths_text)

        (
            text_transformed,
            text_in,
            text_seq_ids,
            lm_mask_bool_text,
        ) = self._create_sequence(
            tf_batch_data[TEXT_SEQUENCE_FEATURES],
            tf_batch_data[TEXT_SENTENCE_FEATURES],
            sequence_mask_text,
            mask_text,
            self.text_name,
            sparse_dropout=self.config[SPARSE_INPUT_DROPOUT],
            dense_dropout=self.config[DENSE_INPUT_DROPOUT],
            masked_lm_loss=self.config[MASKED_LM],
            sequence_ids=True,
        )

        sequence_mask_label = super()._get_mask_for(
            tf_batch_data, LABEL_SEQUENCE_LENGTH
        )
        sequence_lengths_label = self._get_sequence_lengths(
            tf_batch_data, LABEL_SEQUENCE_LENGTH, batch_dim
        )
        mask_label = self._compute_mask(sequence_lengths_label)

        label_transformed, _, _, _ = self._create_sequence(
            tf_batch_data[LABEL_SEQUENCE_FEATURES],
            tf_batch_data[LABEL_SENTENCE_FEATURES],
            sequence_mask_label,
            mask_label,
            self.label_name,
        )

        losses = []

        if self.config[MASKED_LM]:
            loss, acc = self._mask_loss(
                text_transformed,
                text_in,
                text_seq_ids,
                lm_mask_bool_text,
                self.text_name,
            )

            self.mask_loss.update_state(loss)
            self.mask_acc.update_state(acc)
            losses.append(loss)

        # get sentence feature vector for label classification
        sentence_vector_text = self._last_token(text_transformed, sequence_lengths_text)
        sentence_vector_label = self._last_token(
            label_transformed, sequence_lengths_label
        )
        label_ids = tf_batch_data[LABEL_IDS][0]

        loss, acc = self._calculate_label_loss(
            sentence_vector_text, sentence_vector_label, label_ids
        )
        self.response_loss.update_state(loss)
        self.response_acc.update_state(acc)
        losses.append(loss)

        return tf.math.add_n(losses)

    def batch_predict(
        self, batch_in: Union[Tuple[tf.Tensor], Tuple[np.ndarray]]
    ) -> Dict[Text, tf.Tensor]:
        tf_batch_data = self.batch_to_model_data_format(
            batch_in, self.predict_data_signature
        )

        sequence_mask_text = super()._get_mask_for(tf_batch_data, TEXT_SEQUENCE_LENGTH)
        sequence_lengths_text = self._get_sequence_lengths(
            tf_batch_data, TEXT_SEQUENCE_LENGTH, batch_dim=1
        )
        mask_text = self._compute_mask(sequence_lengths_text)

        text_transformed, _, _, _ = self._create_sequence(
            tf_batch_data[TEXT_SEQUENCE_FEATURES],
            tf_batch_data[TEXT_SENTENCE_FEATURES],
            sequence_mask_text,
            mask_text,
            self.text_name,
        )

        out = {}

        if self.all_labels_embed is None:
            _, self.all_labels_embed = self._create_all_labels()

        # get sentence feature vector for intent classification
        sentence_vector = self._last_token(text_transformed, sequence_lengths_text)
        sentence_vector_embed = self._tf_layers[f"embed.{TEXT}"](sentence_vector)

        sim_all = self._tf_layers[f"loss.{LABEL}"].sim(
            sentence_vector_embed[:, tf.newaxis, :],
            self.all_labels_embed[tf.newaxis, :, :],
        )
        scores = self._tf_layers[f"loss.{LABEL}"].confidence_from_sim(
            sim_all, self.config[SIMILARITY_TYPE]
        )
        out["i_scores"] = scores

        return out
