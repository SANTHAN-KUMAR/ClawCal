"""Tool registration.

One place where every capability an agent can reach is listed. If it is not
registered here, no agent can call it.
"""
from .approval import RequestApprovalTool
from .base import Tool, ToolContext, ToolGateway, ToolResult, tools
from .calculator import CalculatorTool, evaluate, task_calculations
from .docgen import (ApprovalNoteTool, PresentationTool, ReportTool,
                     SpreadsheetTool)
from .drawing import AnalyseDrawingTool, TraceConnectionTool
from .files import ListFilesTool, ReadFileTool, WriteFileTool
from .sandbox import EgressProbeTool, SandboxTool, egress_probe, run_code
from .search import (ExtractValuesTool, KnowledgeSearchTool,
                     ListDocumentsTool, ReadDocumentPageTool)

_REGISTERED = False


def register_all() -> ToolGateway:
    global _REGISTERED
    if _REGISTERED:
        return tools
    for tool in (
        CalculatorTool(), KnowledgeSearchTool(), ListDocumentsTool(),
        ReadDocumentPageTool(), ExtractValuesTool(), ListFilesTool(), ReadFileTool(), WriteFileTool(),
        SandboxTool(), AnalyseDrawingTool(), TraceConnectionTool(),
        ApprovalNoteTool(), ReportTool(), SpreadsheetTool(), PresentationTool(),
        RequestApprovalTool(), EgressProbeTool(),
    ):
        tools.register(tool)
    _REGISTERED = True
    return tools


__all__ = ["tools", "register_all", "Tool", "ToolContext", "ToolResult",
           "ToolGateway", "evaluate", "task_calculations", "run_code",
           "egress_probe"]
