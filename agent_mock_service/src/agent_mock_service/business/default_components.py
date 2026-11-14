"""
Sth Demo Default Components
"""


import pathlib
from typing import Any, Dict, Optional, Sequence, Generic, TypeVar, Mapping

import openai
from loguru import logger

from agent_mock_service.mock_core.contracts import (
    BusinessFeatureExtractor, FeatureComparator,
    ContextFusionProvider, Judge, FeatureSetT
)
from agent_mock_service.mock_core.domain import (
    ContextMessage, ScenarioJudgeCandidate, MatchDecision,
    JudgeResult, FeatureRelation, SoftFeaturesT, HardFeaturesT, FeatureSet
)


LLM_CLIENT = ...  # openai.OpenAI()


project_root = pathlib.Path(__file__).parents[1]

DEFAULT_EXTRACT_TEMPLATE_PATH = "..."
DEFAULT_FUSE_PROMPT_PATH = "..."
DEFAULT_JUDGE_PROMPT_PATH = "..."

with open(DEFAULT_EXTRACT_TEMPLATE_PATH, "r", encoding="utf-8") as extract_f:
  DEFAULT_EXTRACT_TEMPLATE = extract_f.read()
with open(DEFAULT_FUSE_PROMPT_PATH, "r", encoding="utf-8") as fuse_f:
  default_fuse_prompt = fuse_f.read()
with open(DEFAULT_JUDGE_PROMPT_PATH, "r", encoding="utf-8") as judge_f:
  default_judge_prompt = judge_f.read()

FeatureT = TypeVar("FeatureT")


class DefaultFeatureExtractor(
    BusinessFeatureExtractor,
    Generic[
        FeatureSetT
    ]
):
    def __init__(
        self,
        hard_feature_schema: type[HardFeaturesT],
        soft_feature_schema: type[SoftFeaturesT],
        feature_set_schema: type[FeatureSetT] = FeatureSetT,
        llm_client: openai.OpenAI = LLM_CLIENT,
        client_model: str = "Qwen",
        extract_hard_features_prompt: Optional[str] = None,
        extract_soft_features_prompt: Optional[str] = None
    ):
        ...

    def extract(self, query: str) -> FeatureSetT:
        ...

    def extract_hard_features(self) -> HardFeaturesT:
        ...

    def extract_soft_features(self) -> SoftFeaturesT:
        ...


class DefaultFeatureComparator(
    FeatureComparator[FeatureSetT],
    Generic[HardFeaturesT, SoftFeaturesT, FeatureSetT]
):
    def __inint__(
        self, 
        hard_feature_schema: type[HardFeaturesT],
        soft_feature_schema: type[SoftFeaturesT],
        feature_set_schema: type[FeatureSetT] = FeatureSetT
    ):
        ...

    def compare(
        self, 
        query: FeatureSetT | dict[str, Any] | str,
        candidate: FeatureSetT | dict[str, Any] | str,
    ) -> tuple[FeatureRelation, float]:
        """返回: 对比结果, 置信度"""
        ...


def DefaultContextFusionProvider(ContextFusionProvider):
    fingerprint = "DefaultContextFusionProvider"
  
    def __init__(
      self,
      llm_client: openai.OpenAI = LLM_CLIENT,
      client_model: str = "Qwen",
      fuse_prompt: Optional[str] = default_fuse_prompt
    ):
        ...

    def fuse(
        self,
        inputs: Sequence[ContextMessage]
    ) -> str:
        ...


def DefaultJudge(Judge):
    model_name = "DefaultJudgeModel"
    prompt_version = "withoutDifferentiatingBusinessScenarioPrompt"
  
    def __init__(
        self,
        llm_client: openai.OpenAI = LLM_CLIENT,
        client_model: str = "Qwen",
        select_prompt: Optional[str] = default_judge_prompt
    ):
        ...

    def judge(
        self,
        query: str,
        candidates: list[ScenarioJudgeCandidate]
    ) -> JudgeResult:
        ...
