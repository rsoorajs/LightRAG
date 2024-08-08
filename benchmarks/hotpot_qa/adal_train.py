"""We will use dspy's retriever to keep that the same and only use our generator and optimizer"""

import dspy
from typing import List, Union, Optional, Dict, Callable
from lightrag.optim.parameter import Parameter, ParameterType

from lightrag.datasets.hotpot_qa import HotPotQA, HotPotQAData
from lightrag.datasets.types import Example

from lightrag.core.retriever import Retriever


colbertv2_wiki17_abstracts = dspy.ColBERTv2(
    url="http://20.102.90.50:2017/wiki17_abstracts"
)

dspy.settings.configure(rm=colbertv2_wiki17_abstracts)


def load_datasets():
    # trainset = HotPotQA(split="train", size=2)
    # valset = HotPotQA(split="val", size=5)
    # testset = HotPotQA(split="test", size=5)
    trainset = HotPotQA(split="train", size=20)
    valset = HotPotQA(split="val", size=50)
    testset = HotPotQA(split="test", size=50)
    print(f"trainset, valset: {len(trainset)}, {len(valset)}, example: {trainset[0]}")
    return trainset, valset, testset


# task pipeline
from typing import Any, Tuple

from lightrag.core import Component, Generator


query_template = """<START_OF_SYSTEM_PROMPT>
Write a simple search query that will help answer a complex question.

You will receive a context and a question. Think step by step.
The last line of your response should be of the following format: 'Query: $VALUE' where VALUE is a search query.

{# Few shot demos #}
{% if few_shot_demos is not none %}
Here are some examples:
{{few_shot_demos}}
{% endif %}
<END_OF_SYSTEM_PROMPT>
<START_OF_USER>
Context: {{context}}
Question: {{question}}
<END_OF_USER>
"""

# Library gives a standard template for easy prompt
answer_template = """<START_OF_SYSTEM_PROMPT>
Answer questions with short factoid answers.

You will receive context and a question. Think step by step.
The last line of your response should be of the following format: 'Answer: $VALUE' where VALUE is a short factoid answer.

{# Few shot demos #}
{% if few_shot_demos is not none %}
Here are some examples:
{{few_shot_demos}}
{% endif %}
<END_OF_SYSTEM_PROMPT>
<START_OF_USER>
Context: {{context}}
Question: {{question}}
"""

from lightrag.core.component import fun_to_component
import re


@fun_to_component
def parse_string_query(text: str) -> str:
    return re.search(r"Query: (.*)", text).group(1)


@fun_to_component
def parse_string_answer(text: str) -> str:
    return re.search(r"Answer: (.*)", text).group(1)


from dataclasses import dataclass, field


@dataclass
class HotPotQADemoData(Example):
    context: List[str] = field(
        metadata={"desc": "The context to be used for answering the question"},
        default_factory=list,
    )
    score: float = field(
        metadata={"desc": "The score of the answer"},
        default=None,
    )


from benchmarks.hotpot_qa.dspy_train import validate_context_and_answer_and_hops


def convert_y_pred_to_dataclass(y_pred):
    # y_pred in both eval and train mode
    context: List[str] = (
        y_pred.input_args["prompt_kwargs"]["context"]
        if hasattr(y_pred, "input_args")
        else []
    )
    # context_str = "\n".join(context)
    data = y_pred.data if hasattr(y_pred, "data") else y_pred
    return DynamicDataClassFactory.from_dict(
        class_name="HotPotQAData",
        data={
            "answer": data,
            "context": context,
        },
    )


def eval_fn(sample, y_pred, metadata):
    if isinstance(sample, Parameter):
        sample = sample.data
    y_pred_obj = convert_y_pred_to_dataclass(y_pred)
    return 1 if validate_context_and_answer_and_hops(sample, y_pred_obj) else 0


from lightrag.core.types import RetrieverOutput, GeneratorOutput


# Demonstrating how to wrap other retriever to adalflow retriever and be applied in training pipeline
class DspyRetriever(Retriever):
    def __init__(self, k=3):
        super().__init__()
        self.k = k
        self.dspy_retriever = dspy.Retrieve(k=k)

    def call(self, input, top_k=None, id=None):
        output = self.dspy_retriever(query_or_queries=input, k=self.k)
        print(f"dsy_retriever output: {output}")
        final_output: List[RetrieverOutput] = []
        documents = output.passages

        final_output.append(
            RetrieverOutput(
                query=input,
                documents=documents,
                doc_indices=[],
            )
        )
        print(f"final_output: {final_output}")
        return final_output


# example need to have question,
# pred needs to have query
from lightrag.optim.text_grad.function import GradFunction


# User customize an auto-grad operator
class MultiHopRetriever(GradFunction):
    def __init__(self, model_client, model_kwargs, passages_per_hop=3, max_hops=2):
        super().__init__()

        self.passages_per_hop = passages_per_hop
        self.max_hops = max_hops

        self.query_generator = Generator(
            name="query_generator",
            model_client=model_client,
            model_kwargs=model_kwargs,
            prompt_kwargs={
                "few_shot_demos": Parameter(
                    alias="few_shot_demos_1",
                    data=None,
                    role_desc="To provide few shot demos to the language model",
                    requires_opt=True,
                    param_type=ParameterType.DEMOS,
                )
            },
            template=query_template,
            output_processors=parse_string_query,
            use_cache=True,
            demo_data_class=HotPotQADemoData,
            demo_data_class_input_mapping={
                "question": "question",
                # "context": "context",
            },
            demo_data_class_output_mapping={"answer": lambda x: x.raw_response},
        )
        self.retrieve = DspyRetriever(k=passages_per_hop)

    def call(self, question: str, id: str = None) -> Any:  # Add id for tracing
        # inferenc mode
        # output = self.forward(question, id=id)
        context = []
        self.max_hops = 1
        # for hop in range(self.max_hops):
        last_context_param = Parameter(
            data=context,
            alias=f"query_context_{id}_{0}",
            requires_opt=True,
        )
        query = self.query_generator(
            prompt_kwargs={
                "context": last_context_param,
                "question": question,
            },
            id=id,
        )
        print(f"query: {query}")
        if isinstance(query, GeneratorOutput):
            query = query.data
        output = self.retrieve(query)
        print(f"output: {output}")
        print(f"output call: {output}")
        return output[0].documents

    def forward(self, question: str, id: str = None) -> Parameter:
        question_param = question
        if not isinstance(question, Parameter):
            question_param = Parameter(
                data=question,
                alias="question",
                role_desc="The question to be answered",
                requires_opt=False,
            )
        context = []
        self.max_hops = 1
        # for hop in range(self.max_hops):
        last_context_param = Parameter(
            data=context,
            alias=f"query_context_{id}_{0}",
            requires_opt=True,
        )
        query = self.query_generator(
            prompt_kwargs={
                "context": last_context_param,
                "question": question_param,
            },
            id=id,
        )
        print(f"query: {query}")
        if isinstance(query, GeneratorOutput):
            query = query.data
        output = self.retrieve(query)
        print(f"output: {output}")
        passages = []
        if isinstance(output, Parameter):
            passages = output.data[0].documents
        else:
            passages = output[0].documents
        # context = deduplicate(context + passages) # all these needs to gradable
        # output_param = Parameter(
        #     data=passages,
        #     alias=f"qa_context_{id}",
        #     role_desc="The context to be used for answering the question",
        #     requires_opt=True,
        # )
        output.data = passages  # reset the values to be used in the next
        if not isinstance(output, Parameter):
            raise ValueError(f"Output must be a Parameter, got {output}")
        return output
        # output_param.set_grad_fn(
        #     BackwardContext(
        #         backward_fn=self.backward,
        #         response=output_param,
        #         id=id,
        #         prededecessors=prededecessors,
        #     )
        # )
        # return output_param

    def backward(self, response: Parameter, id: Optional[str] = None):
        print(f"MultiHopRetriever backward: {response}")
        children_params = response.predecessors
        # backward score to the demo parameter
        for pred in children_params:
            if pred.requires_opt:
                # pred._score = float(response._score)
                pred.set_score(response._score)
                print(
                    f"backpropagate the score {response._score} to {pred.alias}, is_teacher: {self.teacher_mode}"
                )
                if pred.param_type == ParameterType.DEMOS:
                    # Accumulate the score to the demo
                    pred.add_score_to_trace(
                        trace_id=id, score=response._score, is_teacher=self.teacher_mode
                    )
                    print(f"Pred: {pred.alias}, traces: {pred._traces}")


from lightrag.optim.text_grad.function import GradFunction


class HotPotQARAG(
    Component
):  # use component as not creating a new ops, but assemble existing ops
    r"""Same system prompt as text-grad paper, but with our one message prompt template, which has better starting performance"""

    def __init__(self, model_client, model_kwargs, passages_per_hop=3, max_hops=2):
        super().__init__()

        self.passages_per_hop = passages_per_hop
        self.max_hops = max_hops

        self.multi_hop_retriever = MultiHopRetriever(
            model_client=model_client,
            model_kwargs=model_kwargs,
            passages_per_hop=passages_per_hop,
            max_hops=max_hops,
        )
        # TODO: sometimes the cache will collide, so we get different evaluation
        self.llm_counter = Generator(
            name="QuestionAnswering",
            model_client=model_client,
            model_kwargs=model_kwargs,
            prompt_kwargs={
                "few_shot_demos": Parameter(
                    alias="few_shot_demos",
                    data=None,
                    role_desc="To provide few shot demos to the language model",
                    requires_opt=True,
                    param_type=ParameterType.DEMOS,
                )
            },
            template=answer_template,
            output_processors=parse_string_answer,
            use_cache=True,
            demo_data_class=HotPotQADemoData,
            demo_data_class_input_mapping={
                "question": "question",
                "context": "context",
            },
            demo_data_class_output_mapping={"answer": lambda x: x.raw_response},
        )

    # TODO: the error will be a context
    # a component wont handle training, forward or backward, just passing everything through
    def call(self, question: str, id: str = None) -> Union[Parameter, str]:

        # normal component, will be called when in inference mode

        question_param = Parameter(
            data=question,
            alias="question",
            role_desc="The question to be answered",
            requires_opt=False,
        )
        context = []  # noqa: F841
        output = None
        retrieved_context = self.multi_hop_retriever(question_param, id=id)

        # forming a backpropagation graph
        # Make this step traceable too.
        # for hop in range(self.max_hops):
        #     # make context a parameter to be able to trace
        #     query = self.query_generator(
        #         prompt_kwargs={
        #             "context": Parameter(
        #                 data=context, alias=f"query_context_{id}", requires_opt=True
        #             ),
        #             "question": question_param,
        #         },
        #         id=id,
        #     )
        #     print(f"query: {query}")
        #     if isinstance(query, GeneratorOutput):
        #         query = query.data
        #     output = self.retrieve(query)
        #     print(f"output: {output}")
        #     passages = []
        #     if isinstance(output, Parameter):
        #         passages = output.data[0].documents
        #     else:
        #         output[0].documents
        #     context = deduplicate(context + passages)
        # print(f"context: {context}")

        output = self.llm_counter(
            prompt_kwargs={
                "context": retrieved_context,
                "question": question_param,
            },
            id=id,
        )  # already support both training (forward + call)

        if (
            not self.training
        ):  # if users want to customize the output, ensure to use if not self.training

            # convert the generator output to a normal data format
            print(f"converting output: {output}")

            if output.data is None:
                error_msg = (
                    f"Error in processing the question: {question}, output: {output}"
                )
                print(error_msg)
                output = error_msg
            else:
                output = output.data
        return output


from lightrag.optim.trainer.adal import AdalComponent
from lightrag.optim.trainer.trainer import Trainer
from lightrag.optim.few_shot.bootstrap_optimizer import BootstrapFewShot
from lightrag.eval.answer_match_acc import AnswerMatchAcc
from lightrag.optim.text_grad.text_loss_with_eval_fn import EvalFnToTextLoss
from lightrag.core.base_data_class import DynamicDataClassFactory


class HotPotQARAGAdal(AdalComponent):
    # TODO: move teacher model or config in the base class so users dont feel customize too much
    def __init__(self, task: Component, teacher_model_config: dict):
        super().__init__()
        self.task = task
        self.teacher_model_config = teacher_model_config

        self.evaluator = AnswerMatchAcc("fuzzy_match")
        self.eval_fn = self.evaluator.compute_single_item
        # self.eval_fn = eval_fn

    def handle_one_train_sample(
        self, sample: HotPotQAData
    ) -> Any:  # TODO: auto id, with index in call train examples
        return self.task.call, {"question": sample.question, "id": sample.id}

    def handle_one_loss_sample(
        self, sample: HotPotQAData, y_pred: Any
    ) -> Tuple[Callable, Dict]:
        return self.loss_fn, {
            "kwargs": {
                "y": y_pred,
                "y_gt": Parameter(
                    data=sample.answer,
                    role_desc="The ground truth(reference correct answer)",
                    alias="y_gt",
                    requires_opt=False,
                ),
            }
        }

    # def handle_one_loss_sample(
    #     self,
    #     sample: HotPotQAData,
    #     y_pred: Any,
    #     metadata: Optional[Dict[str, Any]] = None,
    # ) -> Tuple[Callable, Dict]:
    #     return self.loss_fn, {
    #         "kwargs": {
    #             "y_pred": y_pred,
    #             "sample": Parameter(
    #                 data=sample,
    #                 role_desc="The ground truth(reference correct answer)",
    #                 alias="y_gt",
    #                 requires_opt=False,
    #             ),
    #             "metadata": metadata,
    #         }
    #     }

    # def handle_one_loss_sample(
    #     self, sample: HotPotQAData, y_pred: Any
    # ) -> Tuple[Callable, Dict]:
    #     return self.loss_fn, {"kwargs": {"y_pred": y_pred, "sample": sample}}

    def configure_optimizers(self, *args, **kwargs):

        # TODO: simplify this, make it accept generator
        parameters = []
        for name, param in self.task.named_parameters():
            param.name = name
            parameters.append(param)
        do = BootstrapFewShot(params=parameters)
        return [do]

    def evaluate_one_sample(
        self, sample: Any, y_pred: Any, metadata: Dict[str, Any]
    ) -> Any:

        # we need "context" be passed as metadata
        # print(f"sample: {sample}, y_pred: {y_pred}")
        # convert pred to Dspy structure

        # y_obj = convert_y_pred_to_dataclass(y_pred)
        # print(f"y_obj: {y_obj}")
        # raise ValueError("Stop here")
        if metadata:
            return self.eval_fn(sample, y_pred, metadata)
        return self.eval_fn(sample, y_pred)

    def configure_teacher_generator(self):
        super().configure_teacher_generator(**self.teacher_model_config)

    def configure_loss_fn(self):
        self.loss_fn = EvalFnToTextLoss(
            eval_fn=self.eval_fn,
            eval_fn_desc="ObjectCountingEvalFn, Output accuracy score: 1 for correct, 0 for incorrect",
            backward_engine=None,
        )


def validate_dspy_demos(
    demos_file="benchmarks/BHH_object_count/models/dspy/hotpotqa.json",
):
    from lightrag.utils.file_io import load_json

    demos_json = load_json(demos_file)

    demos = demos_json["generate_answer"]["demos"]  # noqa: F841

    task = HotPotQARAG(  # noqa: F841
        **gpt_3_model,
        passages_per_hop=3,
        max_hops=2,
    )
    # task.llm_counter.p


if __name__ == "__main__":
    ### Try the minimum effort to test on any task

    # get_logger(level="DEBUG")
    trainset, valset, testset = load_datasets()

    from use_cases.question_answering.bhh_object_count.config import (
        gpt_3_model,
        gpt_4o_model,
    )
    import dspy

    task = HotPotQARAG(
        **gpt_3_model,
        passages_per_hop=3,
        max_hops=2,
    )
    print(task)
    question = "How long is the highway Whitehorse/Cousins Airport was built to support as of 2012?"
    print(task(question))

    # for name, param in task.named_parameters():
    #     print(f"name: {name}, param: {param}")

    trainset, valset, testset = load_datasets()

    trainer = Trainer(
        adaltask=HotPotQARAGAdal(task=task, teacher_model_config=gpt_4o_model),
        max_steps=10,
        raw_shots=0,
        bootstrap_shots=4,
        train_batch_size=4,
        ckpt_path="hotpot_qa_rag",
        strategy="random",
        save_traces=True,
        debug=True,  # make it having debug mode
        weighted_sampling=True,
    )
    # fit include max steps
    trainer.fit(
        train_dataset=trainset, val_dataset=valset, test_dataset=testset, debug=True
    )


# TODO: i forgot that i need demo_data_class
# TODO: i forgot that i need to set id
# Failed to generate demos but no error messages
