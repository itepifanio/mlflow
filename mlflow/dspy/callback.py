import logging
from typing import Any, Dict, Optional

import dspy
from dspy.utils.callback import ACTIVE_CALL_ID, BaseCallback

import mlflow
from mlflow.entities import SpanStatusCode, SpanType
from mlflow.entities.span_event import SpanEvent

_logger = logging.getLogger(__name__)


class MlflowCallback(BaseCallback):
    """Callback for generating MLflow traces for DSPy components"""

    def __init__(self):
        self._client = mlflow.MlflowClient()
        self._call_id_to_span = {}

    def on_module_start(self, call_id: str, instance: Any, inputs: Dict[str, Any]):
        span_type = self._get_span_type_for_module(instance)
        attributes = self._get_span_attribute_for_module(instance)

        # The __call__ method of dspy.Module has a signature of (self, *args, **kwargs),
        # while all built-in modules only accepts keyword arguments. To avoid recording
        # empty "args" key in the inputs, we remove it if it's empty.
        if "args" in inputs and not inputs["args"]:
            inputs.pop("args")

        self._start_span(
            call_id,
            name=f"{instance.__class__.__name__}.forward",
            span_type=span_type,
            inputs=self._unpack_kwargs(inputs),
            attributes=attributes,
        )

    def on_module_end(
        self, call_id: str, outputs: Optional[Any], exception: Optional[Exception] = None
    ):
        # NB: DSPy's Prediction object is a customized dictionary-like object, but its repr
        # is not easy to read on UI. Therefore, we unpack it to a dictionary.
        # https://github.com/stanfordnlp/dspy/blob/6fe693528323c9c10c82d90cb26711a985e18b29/dspy/primitives/prediction.py#L21-L28
        if isinstance(outputs, dspy.Prediction):
            outputs = outputs.toDict()

        self._end_span(call_id, outputs, exception)

    def on_lm_start(self, call_id: str, instance: Any, inputs: Dict[str, Any]):
        span_type = (
            SpanType.CHAT_MODEL if getattr(instance, "model_type", None) == "chat" else SpanType.LLM
        )

        attributes = {
            **instance.kwargs,
            "model": instance.model,
            "model_type": instance.model_type,
            "cache": instance.cache,
        }

        self._start_span(
            call_id,
            name=f"{instance.__class__.__name__}.__call__",
            span_type=span_type,
            inputs=self._unpack_kwargs(inputs),
            attributes=attributes,
        )

    def on_lm_end(
        self, call_id: str, outputs: Optional[Any], exception: Optional[Exception] = None
    ):
        self._end_span(call_id, outputs, exception)

    def on_adapter_format_start(self, call_id: str, instance: Any, inputs: Dict[str, Any]):
        self._start_span(
            call_id,
            name=f"{instance.__class__.__name__}.format",
            span_type=SpanType.PARSER,
            inputs=self._unpack_kwargs(inputs),
            attributes={},
        )

    def on_adapter_format_end(
        self, call_id: str, outputs: Optional[Any], exception: Optional[Exception] = None
    ):
        self._end_span(call_id, outputs, exception)

    def on_adapter_parse_start(self, call_id: str, instance: Any, inputs: Dict[str, Any]):
        self._start_span(
            call_id,
            name=f"{instance.__class__.__name__}.parse",
            span_type=SpanType.PARSER,
            inputs=self._unpack_kwargs(inputs),
            attributes={},
        )

    def on_adapter_parse_end(
        self, call_id: str, outputs: Optional[Any], exception: Optional[Exception] = None
    ):
        self._end_span(call_id, outputs, exception)

    def _start_span(
        self,
        call_id: str,
        name: str,
        span_type: SpanType,
        inputs: Dict[str, Any],
        attributes: Dict[str, Any],
    ):
        # Get parent span in this order:
        # 1. If there is an parent component in DSPy, use its span as parent span.
        # 2. If there is an active span in MLflow, use it as parent span.
        # 3. Otherwise, start a new root span.
        if parent_call_id := ACTIVE_CALL_ID.get():
            parent_span = self._call_id_to_span.get(parent_call_id)
            if not parent_span:
                _logger.warning("Failed to create a span. Parent span not found.")
        elif active_span := mlflow.get_current_active_span():
            parent_span = active_span
        else:
            parent_span = None

        common_params = {
            "name": name,
            "span_type": span_type,
            "inputs": inputs,
            "attributes": attributes,
        }

        if parent_span:
            span = self._client.start_span(
                request_id=parent_span.request_id, parent_id=parent_span.span_id, **common_params
            )
        else:
            span = self._client.start_trace(**common_params)

        self._call_id_to_span[call_id] = span

    def _end_span(
        self,
        call_id: str,
        outputs: Optional[Any],
        exception: Optional[Exception] = None,
    ):
        span = self._call_id_to_span.pop(call_id, None)

        if not span:
            _logger.warning(f"Failed to end a span. Span not found for call_id: {call_id}")
            return

        status = SpanStatusCode.OK if exception is None else SpanStatusCode.ERROR

        if exception:
            span.add_event(SpanEvent.from_exception(exception))

        common_params = {
            "request_id": span.request_id,
            "outputs": outputs,
            "status": status,
        }

        if span.parent_id:
            self._client.end_span(span_id=span.span_id, **common_params)
        else:
            self._client.end_trace(**common_params)

    def _get_span_type_for_module(self, instance):
        if isinstance(instance, dspy.Retrieve):
            return SpanType.RETRIEVER
        elif isinstance(instance, dspy.ReAct):
            return SpanType.AGENT
        elif isinstance(instance, dspy.Predict):
            return SpanType.LLM
        elif isinstance(instance, dspy.Adapter):
            return SpanType.PARSER
        else:
            return SpanType.CHAIN

    def _get_span_attribute_for_module(self, instance):
        if isinstance(instance, dspy.Predict):
            return {"signature": instance.signature.signature}
        elif isinstance(instance, dspy.ChainOfThought):
            return {
                "signature": instance.signature.signature,
                "extended_signature": instance.extended_signature.signature,
            }
        return {}

    def _unpack_kwargs(self, inputs: Dict[str, Any]) -> Dict[str, Any]:
        """Unpacks the kwargs from the inputs dictionary"""
        # NB: Not using pop() to avoid modifying the original inputs dictionary
        kwargs = inputs.get("kwargs", {})
        inputs_wo_kwargs = {k: v for k, v in inputs.items() if k != "kwargs"}
        return {**inputs_wo_kwargs, **kwargs}
