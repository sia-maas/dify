import json
from collections.abc import Mapping, Sequence
from typing import Any, Optional, cast

from core.app.entities.app_invoke_entities import ModelConfigWithCredentialsEntity
from core.memory.token_buffer_memory import TokenBufferMemory
from core.model_manager import ModelInstance
from core.model_runtime.entities import LLMUsage, ModelPropertyKey, PromptMessageRole
from core.model_runtime.utils.encoders import jsonable_encoder
from core.prompt.advanced_prompt_transform import AdvancedPromptTransform
from core.prompt.simple_prompt_transform import ModelMode
from core.prompt.utils.prompt_message_util import PromptMessageUtil
from core.workflow.entities.node_entities import NodeRunMetadataKey, NodeRunResult
from core.workflow.nodes.enums import NodeType
from core.workflow.nodes.event import ModelInvokeCompletedEvent
from core.workflow.nodes.llm import (
    LLMNode,
    LLMNodeChatModelMessage,
    LLMNodeCompletionModelPromptTemplate,
)
from core.workflow.utils.variable_template_parser import VariableTemplateParser
from libs.json_in_md_parser import parse_and_check_json_markdown
from models.workflow import WorkflowNodeExecutionStatus

from .entities import TextTo3DNodeData
from .exc import InvalidModelTypeError
from .template_prompts import (
    QUESTION_3D_ASSISTANT_PROMPT_1,
    QUESTION_3D_COMPLETION_PROMPT,
    QUESTION_3D_SYSTEM_PROMPT,
    QUESTION_3D_USER_PROMPT_1,
)
import requests


class TextTo3DNode(LLMNode):
    _node_data_cls = TextTo3DNodeData  # type: ignore
    _node_type = NodeType.TEXT_TO_3D

    def _run(self):
        node_data = cast(TextTo3DNodeData, self.node_data)
        variable_pool = self.graph_runtime_state.variable_pool
        
        #配置参数应该从大模型配置中获取
        token = "api-75220322-0960-4e55-b72d-02286bf183c7"
        url = "http://127.0.0.1:5771"
        
        # 暂时从原来节点的变量中获取需要的参数
        query = node_data.classes[0].name
        output_format = node_data.classes[1].name

        # extract variables
        variables = {"query": query}
        # fetch model config
        model_instance, model_config = self._fetch_model_config(node_data.model)
        # fetch memory
        memory = self._fetch_memory(
            node_data_memory=node_data.memory,
            model_instance=model_instance,
        )
        
        files = (
            self._fetch_files(
                selector=node_data.vision.configs.variable_selector,
            )
            if node_data.vision.enabled
            else []
        )

        # fetch prompt messages
        rest_token = self._calculate_rest_token(
            node_data=node_data,
            query=query or "",
            model_config=model_config,
            context="",
        )
        prompt_template = self._get_prompt_template(
            node_data=node_data,
            query=query or "",
            memory=memory,
            max_token_limit=rest_token,
        )
        prompt_messages, stop = self._fetch_prompt_messages(
            prompt_template=prompt_template,
            sys_query=query,
            memory=memory,
            model_config=model_config,
            sys_files=files,
            vision_enabled=node_data.vision.enabled,
            vision_detail=node_data.vision.configs.detail,
            variable_pool=variable_pool,
            jinja2_variables=[],
        )

        result_text = ""
        usage = LLMUsage.empty_usage()
        finish_reason = None

        try:
            # handle invoke result
            generator = self._invoke_llm(
                node_data_model=node_data.model,
                model_instance=model_instance,
                prompt_messages=prompt_messages,
                stop=stop,
            )

            for event in generator:
                if isinstance(event, ModelInvokeCompletedEvent):
                    result_text = event.text
                    usage = event.usage
                    finish_reason = event.finish_reason
                    break
            
            outputs = self._generate_3d(url, result_text, token, output_format)
            
            #需要把3D部分的消耗加入进来
            process_data = {
                "model_mode": model_config.mode,
                "prompts": PromptMessageUtil.prompt_messages_to_prompt_for_saving(
                    model_mode=model_config.mode, prompt_messages=prompt_messages
                ),
                "usage": jsonable_encoder(usage),
                "finish_reason": finish_reason,
            }

            return NodeRunResult(
                status=WorkflowNodeExecutionStatus.SUCCEEDED,
                inputs=variables,
                process_data=process_data,
                outputs=outputs,
                metadata={
                    NodeRunMetadataKey.TOTAL_TOKENS: usage.total_tokens,
                    NodeRunMetadataKey.TOTAL_PRICE: usage.total_price,
                    NodeRunMetadataKey.CURRENCY: usage.currency,
                },
                llm_usage=usage,
            )
        except ValueError as e:
            return NodeRunResult(
                status=WorkflowNodeExecutionStatus.FAILED,
                inputs=variables,
                error=str(e),
                metadata={
                    NodeRunMetadataKey.TOTAL_TOKENS: usage.total_tokens,
                    NodeRunMetadataKey.TOTAL_PRICE: usage.total_price,
                    NodeRunMetadataKey.CURRENCY: usage.currency,
                },
                llm_usage=usage,
            )

    @classmethod
    def _extract_variable_selector_to_variable_mapping(
        cls,
        *,
        graph_config: Mapping[str, Any],
        node_id: str,
        node_data: Any,
    ) -> Mapping[str, Sequence[str]]:
        """
        Extract variable selector to variable mapping
        :param graph_config: graph config
        :param node_id: node id
        :param node_data: node data
        :return:
        """
        node_data = cast(TextTo3DNodeData, node_data)
        variable_mapping = {"query": node_data.query_variable_selector}
        variable_selectors = []
        if node_data.instruction:
            variable_template_parser = VariableTemplateParser(template=node_data.instruction)
            variable_selectors.extend(variable_template_parser.extract_variable_selectors())
        for variable_selector in variable_selectors:
            variable_mapping[variable_selector.variable] = variable_selector.value_selector

        variable_mapping = {node_id + "." + key: value for key, value in variable_mapping.items()}

        return variable_mapping

    @classmethod
    def get_default_config(cls, filters: Optional[dict] = None) -> dict:
        """
        Get default config of node.
        :param filters: filter by node config parameters.
        :return:
        """
        return {"type": "text-to-3d", "config": {"instructions": ""}}
    
    def _generate_3d(
        self,
        url : str,
        prompt : str,
        token : str,
        output_format : str
    ) -> str:
        header = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Encoding": "gzip, deflate",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Connection": "keep-alive",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/92.0.4515.159 Safari/537.36"
        }
        request_url = url + "/text_to_cad?prompt=" + prompt + "&token=" + token + "&file_export_format=" + output_format
        http_result = requests.get(request_url, json={}, headers=header)
        return http_result.text

    def _calculate_rest_token(
        self,
        node_data: TextTo3DNodeData,
        query: str,
        model_config: ModelConfigWithCredentialsEntity,
        context: Optional[str],
    ) -> int:
        prompt_transform = AdvancedPromptTransform(with_variable_tmpl=True)
        prompt_template = self._get_prompt_template(node_data, query, None, 2000)
        prompt_messages = prompt_transform.get_prompt(
            prompt_template=prompt_template,
            inputs={},
            query="",
            files=[],
            context=context,
            memory_config=node_data.memory,
            memory=None,
            model_config=model_config,
        )
        rest_tokens = 2000

        model_context_tokens = model_config.model_schema.model_properties.get(ModelPropertyKey.CONTEXT_SIZE)
        if model_context_tokens:
            model_instance = ModelInstance(
                provider_model_bundle=model_config.provider_model_bundle, model=model_config.model
            )

            curr_message_tokens = model_instance.get_llm_num_tokens(prompt_messages)

            max_tokens = 0
            for parameter_rule in model_config.model_schema.parameter_rules:
                if parameter_rule.name == "max_tokens" or (
                    parameter_rule.use_template and parameter_rule.use_template == "max_tokens"
                ):
                    max_tokens = (
                        model_config.parameters.get(parameter_rule.name)
                        or model_config.parameters.get(parameter_rule.use_template or "")
                    ) or 0

            rest_tokens = model_context_tokens - max_tokens - curr_message_tokens
            rest_tokens = max(rest_tokens, 0)

        return rest_tokens

    def _get_prompt_template(
        self,
        node_data: TextTo3DNodeData,
        query: str,
        memory: Optional[TokenBufferMemory],
        max_token_limit: int = 2000,
    ):
        model_mode = ModelMode.value_of(node_data.model.mode)
        input_text = query
        memory_str = ""
        if memory:
            memory_str = memory.get_history_prompt_text(
                max_token_limit=max_token_limit,
                message_limit=node_data.memory.window.size if node_data.memory and node_data.memory.window else None,
            )
        prompt_messages: list[LLMNodeChatModelMessage] = []
        if model_mode == ModelMode.CHAT:
            system_prompt_messages = LLMNodeChatModelMessage(
                role=PromptMessageRole.SYSTEM, text=QUESTION_3D_SYSTEM_PROMPT.format(histories=memory_str)
            )
            prompt_messages.append(system_prompt_messages)
            user_prompt_message_1 = LLMNodeChatModelMessage(
                role=PromptMessageRole.USER, text=QUESTION_3D_USER_PROMPT_1
            )
            prompt_messages.append(user_prompt_message_1)
            assistant_prompt_message_1 = LLMNodeChatModelMessage(
                role=PromptMessageRole.ASSISTANT, text=QUESTION_3D_ASSISTANT_PROMPT_1
            )
            prompt_messages.append(assistant_prompt_message_1)
            return prompt_messages
        elif model_mode == ModelMode.COMPLETION:
            return LLMNodeCompletionModelPromptTemplate(
                text=QUESTION_3D_COMPLETION_PROMPT.format(
                    histories=memory_str,
                    input_text=input_text,
                )
            )

        else:
            raise InvalidModelTypeError(f"Model mode {model_mode} not support.")
