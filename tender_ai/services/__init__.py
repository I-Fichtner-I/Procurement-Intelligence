"""Anwendungsdienste: die Ablaeufe hinter den Oberflaechen.

Die CLI - und spaeter das Dashboard (Stufe 6) - rufen ausschliesslich diese
Funktionen auf. Sie kapseln HTTP-Client-Aufbau, Quellenauswahl, Session-
Handling und Fehlerfaelle; die Oberflaechen kuemmern sich nur noch um Ein- und
Ausgabe. Hier wird bewusst nichts formatiert (kein Rich, keine Konsole).
"""

from .analysis import BatchAnalysisReport, analyze_open_tenders, analyze_tender
from .approval import (
    ApprovalState,
    DraftResult,
    approval_state,
    create_offer_draft,
    pipeline_status,
    record_decision,
)
from .calculation import (
    BatchCalculationReport,
    calculate_open_tenders,
    calculate_tender,
)
from .documents import DocumentReport, DocumentResult, documents_from_db, fetch_documents
from .health import check_sources
from .items import BatchItemReport, extract_items_for_open_tenders, extract_tender_items
from .pipeline import (
    STAGES,
    PipelineReport,
    StageReport,
    resolve_stages,
    run_pipeline,
    run_pipeline_sync,
)
from .pricing import (
    BatchPricingReport,
    research_and_store,
    research_open_tenders,
    research_prices,
)
from .search import run_search

__all__ = [
    "STAGES",
    "BatchAnalysisReport",
    "ApprovalState",
    "BatchCalculationReport",
    "BatchItemReport",
    "BatchPricingReport",
    "DocumentReport",
    "DraftResult",
    "DocumentResult",
    "PipelineReport",
    "StageReport",
    "analyze_open_tenders",
    "analyze_tender",
    "calculate_open_tenders",
    "calculate_tender",
    "approval_state",
    "check_sources",
    "create_offer_draft",
    "documents_from_db",
    "extract_items_for_open_tenders",
    "extract_tender_items",
    "fetch_documents",
    "pipeline_status",
    "record_decision",
    "research_and_store",
    "research_open_tenders",
    "research_prices",
    "resolve_stages",
    "run_pipeline",
    "run_pipeline_sync",
    "run_search",
]
