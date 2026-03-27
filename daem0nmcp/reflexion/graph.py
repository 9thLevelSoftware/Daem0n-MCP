"""
LangGraph State Machine for Reflexion Loop.

Builds the Actor-Evaluator-Reflector graph with conditional edges.
Uses AsyncSqliteSaver for checkpoint persistence.

Graph structure:
  START -> Actor -> Evaluator -> [should_continue?] -> Reflector -> Actor
                               -> [not continue?] -> END
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from langgraph.graph import END, START, StateGraph

from .nodes import create_actor_node, create_evaluator_node, create_reflector_node
from .state import ReflexionState

if TYPE_CHECKING:
    from ..graph import KnowledgeGraph
    from ..memory import MemoryManager

logger = logging.getLogger(__name__)


def should_continue(state: ReflexionState) -> str:
    """
    Conditional edge function: decide whether to continue loop or exit.

    Returns:
        "reflector" to continue loop
        "__end__" to exit
    """
    if state.get("should_continue", False):
        return "reflector"
    return END


def build_reflexion_graph(
    memory_manager: MemoryManager,
    knowledge_graph: KnowledgeGraph | None = None,
    llm_func: Callable[[str], str] | None = None,
    sandbox_executor: Any | None = None,
) -> StateGraph:
    """
    Build the Reflexion StateGraph.

    Args:
        memory_manager: MemoryManager for claim verification
        knowledge_graph: Optional KnowledgeGraph for entity verification
        llm_func: Optional LLM function for Actor (if None, uses placeholder)

    Returns:
        Configured StateGraph (not yet compiled)
    """
    # Create node functions
    actor_node = create_actor_node(llm_func=llm_func)
    evaluator_node = create_evaluator_node(
        memory_manager=memory_manager,
        knowledge_graph=knowledge_graph,
        sandbox_executor=sandbox_executor,
    )
    reflector_node = create_reflector_node()

    # Build graph
    graph = StateGraph(ReflexionState)

    # Add nodes
    graph.add_node("actor", actor_node)
    graph.add_node("evaluator", evaluator_node)
    graph.add_node("reflector", reflector_node)

    # Add edges
    graph.add_edge(START, "actor")
    graph.add_edge("actor", "evaluator")
    graph.add_conditional_edges(
        "evaluator",
        should_continue,
        {"reflector": "reflector", END: END},
    )
    graph.add_edge("reflector", "actor")

    logger.debug("Built Reflexion StateGraph")

    return graph


async def create_reflexion_app(
    memory_manager: MemoryManager,
    knowledge_graph: KnowledgeGraph | None = None,
    llm_func: Callable[[str], str] | None = None,
    checkpoint_path: str | None = None,
    sandbox_executor: Any | None = None,
):
    """
    Create a compiled Reflexion app with optional checkpointing.

    Args:
        memory_manager: MemoryManager for claim verification
        knowledge_graph: Optional KnowledgeGraph for entity verification
        llm_func: Optional LLM function for Actor
        checkpoint_path: Optional path to SQLite checkpoint DB

    Returns:
        Compiled LangGraph app ready for invocation
    """
    graph = build_reflexion_graph(
        memory_manager=memory_manager,
        knowledge_graph=knowledge_graph,
        llm_func=llm_func,
        sandbox_executor=sandbox_executor,
    )

    if checkpoint_path:
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        checkpointer = AsyncSqliteSaver.from_conn_string(checkpoint_path)
        app = graph.compile(checkpointer=checkpointer)
        logger.info(f"Compiled Reflexion app with checkpointing at {checkpoint_path}")
    else:
        app = graph.compile()
        logger.info("Compiled Reflexion app without checkpointing")

    return app


async def run_reflexion(
    query: str,
    memory_manager: MemoryManager,
    knowledge_graph: KnowledgeGraph | None = None,
    llm_func: Callable[[str], str] | None = None,
    thread_id: str | None = None,
    sandbox_executor: Any | None = None,
) -> dict[str, Any]:
    """
    Run the Reflexion loop on a query.

    Convenience function that creates app and invokes it.

    Args:
        query: The query to respond to
        memory_manager: MemoryManager for claim verification
        knowledge_graph: Optional KnowledgeGraph for entity verification
        llm_func: Optional LLM function for Actor
        thread_id: Optional thread ID for checkpointing

    Returns:
        Final state with draft, quality_score, verification_results, etc.
    """
    app = await create_reflexion_app(
        memory_manager=memory_manager,
        knowledge_graph=knowledge_graph,
        llm_func=llm_func,
        sandbox_executor=sandbox_executor,
    )

    initial_state = {
        "query": query,
        "draft": "",
        "critique": "",
        "quality_score": 0.0,
        "claims": [],
        "verification_results": [],
        "iteration": 0,
        "should_continue": True,
        "context_filter": None,
        "code_executions_used": 0,
        "max_code_executions": 2,
        "code_verification_results": [],
        "verification_code": None,
    }

    config: dict[str, Any] = {}
    if thread_id:
        config["configurable"] = {"thread_id": thread_id}

    result = await app.ainvoke(initial_state, config=config)

    return result


__all__ = [
    "build_reflexion_graph",
    "create_reflexion_app",
    "run_reflexion",
    "should_continue",
]
