# TODO: move to utils
import logging
from collections import defaultdict
from typing import Callable, Dict, List, Optional, Type, Union, get_args, get_origin

import jsonref
import yaml

from robusta.core.playbooks.actions_registry import Action
from robusta.core.playbooks.base_trigger import BaseTrigger, ExecutionBaseEvent
from robusta.core.playbooks.trigger import Trigger
from robusta.integrations.kubernetes.autogenerated.events import KubernetesAnyChangeEvent, KubernetesResourceEvent
from robusta.utils.json_schema import example_from_schema


def get_possible_types(t):
    """
    Given a type or a Union of types, returns a list of the actual types
    """
    if get_origin(t) == Union:
        return get_args(t)
    else:
        return [t]


# see https://stackoverflow.com/questions/13518819/avoid-references-in-pyyaml
class NoAliasDumper(yaml.SafeDumper):
    def ignore_aliases(self, data):
        return True


class ExamplesGenerator:

    ANY_TRIGGER_MARKER = "any trigger"

    def __init__(self):
        self.events_to_triggers = defaultdict(set)
        self.triggers_to_yaml = {}

        for field_name, field in Trigger.__fields__.items():
            trigger_classes = [t for t in get_possible_types(field.type_) if issubclass(t, BaseTrigger)]
            if len(trigger_classes) == 0:
                continue

            for t in trigger_classes:
                self.triggers_to_yaml[t] = field_name
                execution_event = t.get_execution_event_type()
                possible_events = [execution_event] + list(
                    cls for cls in execution_event.__mro__ if issubclass(cls, ExecutionBaseEvent)
                )
                for e in possible_events:
                    self.events_to_triggers[e].add(t)

    def get_possible_triggers(self, event_cls: Type[ExecutionBaseEvent]) -> List[str]:
        name = event_cls.__name__
        # TODO: why?
        if name == "ExecutionBaseEvent":
            return ["on_pod_create"]
        triggers = self.events_to_triggers.get(event_cls)
        if triggers is None:
            raise Exception(f"Don't know how to generate an example trigger for {name}")
        return [self.triggers_to_yaml[t] for t in triggers]

    @classmethod
    def get_manual_trigger_cmd(cls, action: Action) -> str:
        action_params_sample = ""
        if action.params_type:
            required: List[str] = action.params_type.schema().get("required", [])
            for field in required:
                action_params_sample = action_params_sample + f" {field}={field.upper()}"

        if action.event_type.__name__ == "ExecutionBaseEvent":
            return f"robusta playbooks trigger {action.action_name} {action_params_sample}"

        from_params_parameters_class = getattr(action, "from_params_parameter_class", None)
        if not from_params_parameters_class:
            return ""

        cmd = f"robusta playbooks trigger {action.action_name}"
        from_params_k8s_type = from_params_parameters_class.schema()["title"].replace("Attributes", "").upper()
        required_fields: List[str] = from_params_parameters_class.schema()["required"]
        for field in required_fields:
            cmd = cmd + f" {field}={from_params_k8s_type}_{field.upper()}"

        return cmd + f" {action_params_sample}"

    def get_supported_triggers(self, action: Action) -> List[str]:
        """
        Get supported triggers for docs.
        Return a list of all supported triggers
        """
        event_cls: Type[ExecutionBaseEvent] = action.event_type
        name = event_cls.__name__
        if name == "ExecutionBaseEvent":
            return [self.ANY_TRIGGER_MARKER]

        all_triggers = self.get_possible_triggers(event_cls)

        # remove duplications and sort
        return list(sorted(list(set(all_triggers))))

    def generate_example_config(
        self,
        action_func: Callable,
        suggested_trigger: Optional[str],
        trigger_params: Optional[Dict[str, str]] = None,
    ):
        action_metadata = Action(action_func)
        if suggested_trigger is not None:
            trigger = suggested_trigger
        else:
            trigger = self.get_possible_triggers(action_metadata.event_type)[0]

        if trigger_params is None:
            trigger_params = {}

        example = {
            "customPlaybooks": [
                {"actions": [{action_metadata.action_name: {}}], "triggers": [{trigger: trigger_params}]}
            ]
        }
        if action_metadata.params_type:
            action_model = action_metadata.params_type
            # instead of loading the schema as python object directly with action_model.schema()
            # we dump to json and then read-back from json to python using jsonref
            # this is necessary to fix parts of the schema that refer to one another
            # without it we need to understand json references and handle them ourselves
            action_schema = jsonref.loads(action_model.schema_json())
            action_example = example_from_schema(action_schema)
            example["customPlaybooks"][0]["actions"][0][action_metadata.action_name] = action_example
        return yaml.dump(example, Dumper=NoAliasDumper)