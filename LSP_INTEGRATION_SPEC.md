# LSP Integration Feasibility Analysis & Implementation Gameplan

**Document Version:** 1.0  
**Date:** 2026-02-09  
**Status:** Draft for Stakeholder Review  
**Author:** Technical Architecture Analysis  

---

## Executive Summary

This document presents a comprehensive analysis of integrating Language Server Protocol (LSP) functionality into Daem0nMCP. Based on research into Serena's LSP implementation, Daem0nMCP's current architecture, and LSP-MCP integration patterns, we recommend a **Direct LSP Client approach** using Microsoft's multilspy library. This strategy enables 100+ language support with minimal maintenance burden while leveraging Daem0nMCP's unique persistent memory and graph capabilities.

**Key Recommendation:** Proceed with Phase 1 implementation. The integration is technically feasible, architecturally compatible, and provides significant competitive differentiation through memory-aware code intelligence.

---

## 1. FEASIBILITY ASSESSMENT

### 1.1 Compatibility Analysis

#### Architecture Fit: EXCELLENT

Daem0nMCP's 6-layer architecture provides natural integration points for LSP:

| Layer | Current Function | LSP Integration Point |
|-------|------------------|----------------------|
| **MCP Transport** | Tool registration via `@mcp.tool()` | Register LSP tools alongside existing tools |
| **Workflow Dispatchers** | 8 consolidated workflows | Add `lsp` workflow or extend `understand` |
| **Context Management** | Per-project contexts | LSP client per project context |
| **Core Managers** | Memory, rules, indexing | Link LSP symbols to memory graph |
| **Code Understanding** | Tree-sitter indexing | **Complement** with LSP semantic analysis |
| **Infrastructure** | SQLite, Qdrant, logging | Store LSP symbol cache, diagnostics history |

**Compatibility Score:** 9/10

The existing `understand` workflow action-based pattern (`index`, `find`, `impact`, `todos`) maps perfectly to LSP features. The `@mcp.tool()` decorator system allows seamless registration of 15-25 new LSP tools.

#### Existing Code Understanding vs LSP

**Current State (Tree-sitter based):**
- **Strengths:** Fast indexing, 13 languages supported, works offline, syntax-based entity extraction
- **Limitations:** No semantic understanding, no type information, no cross-file references without manual analysis
- **Storage:** Entities in SQLite with vector search via Qdrant

**LSP Augmentation:**
- **Adds:** Go-to-definition, find-references, type information, diagnostics, hover docs, refactoring
- **Complements:** Tree-sitter provides fast local analysis; LSP provides deep semantic understanding
- **Synergy:** LSP symbol resolution + Daem0nMCP memory = context-aware code intelligence

### 1.2 Feature Overlap Analysis

#### What Serena Has That Daem0nMCP Lacks (Current State)

| Feature Category | Serena Implementation | Daem0nMCP Gap |
|-----------------|----------------------|---------------|
| **Symbol Resolution** | 30+ LSP tools (find_symbol, find_referencing_symbols) | Tree-sitter only - no semantic resolution |
| **Cross-file Navigation** | Type hierarchy, go-to-definition | Limited to indexed entities |
| **Code Editing** | insert_after_symbol, replace_symbol_body | Manual editing only |
| **Refactoring** | rename_symbol via LSP | No automated refactoring |
| **Diagnostics** | Real-time error reporting | Post-hoc TODO scanning only |
| **Language Coverage** | 30+ languages via parallel LSP | 13 languages via tree-sitter |
| **Language Server Management** | Automatic download, lifecycle mgmt | N/A |

**Gap Priority (High → Low):**
1. **HIGH:** Symbol resolution and cross-file navigation
2. **HIGH:** Real-time diagnostics
3. **MEDIUM:** Refactoring support
4. **MEDIUM:** Code editing via LSP
5. **LOW:** Additional language support (tree-sitter covers most)

#### What Daem0nMCP Has That Serena Lacks

| Capability | Daem0nMCP Implementation | Unique Value |
|-----------|-------------------------|--------------|
| **Persistent Memory Graph** | 2,290+ memories with relationships | LSP symbols linked to project history |
| **Decision Tracking** | 19 tracked decisions with outcomes | "Why was this function written this way?" |
| **Code-Memory Integration** | `propose_refactor` with causal chains | Context-aware refactoring suggestions |
| **Temporal Understanding** | `trace_causal_path`, `trace_evolution` | Track how symbols evolved over time |
| **Entity Linking** | Automatic entity extraction + linking | Connect code symbols to project concepts |
| **Multi-project Federation** | `link_projects`, `consolidate_linked_databases` | Cross-repo symbol resolution |
| **Cognitive Tools** | `simulate_decision`, `debate_internal` | AI-driven code analysis |
| **Dreaming/Background Processing** | Idle-time memory consolidation | Continuous LSP index optimization |

**Competitive Advantage:** Daem0nMCP + LSP = Semantic code understanding with historical context. Serena provides static LSP tools; Daem0nMCP provides *evolving* code intelligence enriched by project memory.

### 1.3 Risk Assessment

#### Technical Risks

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| **LSP Process Management** | Medium | High | Use multilspy (Microsoft) with proven process mgmt |
| **Language Server Installation** | Medium | Medium | Auto-download via multilspy; fallback to manual config |
| **Performance Overhead** | Low | Medium | Lazy LSP initialization; cache symbol results |
| **Async/Sync Bridge** | Low | High | multilspy provides sync API wrapper |
| **Tree-sitter Conflicts** | Low | Low | Complementary systems; no direct conflict |

**Overall Technical Risk: LOW-MEDIUM**

The primary risk (process management) is mitigated by using Microsoft's multilspy library, which handles LSP lifecycle, auto-downloads language servers, and provides both sync and async APIs.

#### Maintenance Burden Assessment

| Component | Maintenance Level | Rationale |
|-----------|------------------|-----------|
| **LSP Client Core** | LOW | multilspy maintained by Microsoft Research |
| **Language Server Configs** | LOW | Community-driven (pyright, tsserver, etc.) |
| **Daem0nMCP LSP Tools** | MEDIUM | ~15-20 tool wrappers; standard pattern |
| **Symbol Cache Integration** | LOW | Extend existing Qdrant/SQLite infrastructure |
| **Diagnostics Pipeline** | MEDIUM | New feature; requires monitoring |

**Overall Maintenance Burden: MEDIUM**

Approximately 20% additional maintenance overhead, primarily in keeping LSP tool wrappers current with protocol updates. This is offset by multilspy handling the complex LSP client lifecycle.

#### Complexity Analysis

**Integration Complexity Breakdown:**

```
Phase 1 (Foundation):        ████████░░  Medium   - LSP client setup, config
Phase 2 (Core Features):     ████████░░  Medium   - 10-12 basic LSP tools
Phase 3 (Advanced):          ██████████  High     - Refactoring, diagnostics
Phase 4 (Memory-Aware):      ████████░░  Medium   - Link LSP to memory graph

Total Complexity:            ████████░░  Medium-High (manageable)
```

**Key Complexity Factors:**
1. **Process orchestration:** Multiple LSP processes (one per language) - handled by multilspy
2. **State synchronization:** Keeping symbol cache in sync with code changes - leverage existing watcher
3. **Async coordination:** MCP tools are async; LSP is async - natural fit

---

## 2. INTEGRATION STRATEGY

### 2.1 Recommended Approach: Direct LSP Client (via multilspy)

**Decision:** Use **Approach A: Direct LSP Client** with Microsoft's multilspy library.

**Rationale:**
- ✅ Supports 100+ languages via standard LSP servers
- ✅ multilspy handles auto-download, lifecycle, sync/async APIs
- ✅ Lowest maintenance burden (Microsoft-maintained)
- ✅ Fastest time-to-value
- ✅ Fits naturally into existing async architecture
- ❌ Requires external process management (mitigated by multilspy)

**Rejected Alternatives:**
- **Embedded LSP Server:** Too high maintenance; defeats purpose of LSP ecosystem
- **Hybrid Approach:** Unnecessary complexity for v1; can add embedded later for custom features

### 2.2 Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           MCP CLIENT (Claude/OpenCode)                       │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼ MCP Protocol
┌─────────────────────────────────────────────────────────────────────────────┐
│                         DAEM0NMCP SERVER (FastMCP)                           │
│  ┌───────────────────────────────────────────────────────────────────────┐  │
│  │                        TOOL REGISTRY (@mcp.tool)                       │  │
│  │  ┌──────────────┐ ┌──────────────┐ ┌──────────────┐ ┌──────────────┐  │  │
│  │  │   COMMUNE    │ │   CONSULT    │ │   INSCRIBE   │ │    LSP       │  │  │
│  │  │  (existing)  │ │  (existing)  │ │  (existing)  │ │   (NEW)      │  │  │
│  │  └──────────────┘ └──────────────┘ └──────────────┘ └──────────────┘  │  │
│  └───────────────────────────────────────────────────────────────────────┘  │
│                                      │                                      │
│                                      ▼                                      │
│  ┌───────────────────────────────────────────────────────────────────────┐  │
│  │                    PROJECT CONTEXT MANAGER                             │  │
│  │              (one LSP client pool per project)                         │  │
│  └───────────────────────────────────────────────────────────────────────┘  │
│                                      │                                      │
│                                      ▼                                      │
│  ┌───────────────────────────────────────────────────────────────────────┐  │
│  │                     LSP CLIENT POOL (multilspy)                        │  │
│  │  ┌────────────┐ ┌────────────┐ ┌────────────┐ ┌─────────────────────┐  │  │
│  │  │  pyright   │ │ tsserver   │ │ rust-anlzr │ │    ... 30+ more     │  │  │
│  │  │   (Python) │ │ (TS/JS)    │ │   (Rust)   │ │                     │  │  │
│  │  └────────────┘ └────────────┘ └────────────┘ └─────────────────────┘  │  │
│  └───────────────────────────────────────────────────────────────────────┘  │
│                                      │                                      │
│  ┌───────────────────────────────────────────────────────────────────────┐  │
│  │                    SYMBOL CACHE & MEMORY GRAPH                         │  │
│  │  ┌────────────┐  ┌────────────┐  ┌────────────┐  ┌────────────┐       │  │
│  │  │  SQLite    │  │  Qdrant    │  │  Memory    │  │  Entity    │       │  │
│  │  │  (metadata)│  │  (vectors) │  │  (history) │  │  (links)   │       │  │
│  │  └────────────┘  └────────────┘  └────────────┘  └────────────┘       │  │
│  └───────────────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 2.3 Key Integration Points

#### Integration 1: LSP Tools → MCP Registry

**Location:** `daem0nmcp/tools/lsp_tools.py` (new file)

**Pattern:** Follow existing tool registration:
```python
@mcp.tool(version=__version__)
@with_request_id
async def lsp_goto_definition(
    file_path: str,
    line: int,
    column: int,
    project_path: Optional[str] = None
) -> Dict[str, Any]:
    """Navigate to symbol definition using LSP."""
    ...
```

#### Integration 2: LSP Client → Project Context

**Location:** `daem0nmcp/context_manager.py` (extension)

**Pattern:** Extend `ProjectContext` to include LSP client pool:
```python
class ProjectContext:
    def __init__(self, ...):
        ...
        self.lsp_manager: Optional[LSPManager] = None
    
    async def get_lsp_manager(self) -> LSPManager:
        if self.lsp_manager is None:
            self.lsp_manager = await LSPManager.create(self.project_path)
        return self.lsp_manager
```

#### Integration 3: Symbol Results → Memory Graph

**Location:** `daem0nmcp/lsp/symbol_bridge.py` (new file)

**Pattern:** When LSP returns symbols, auto-link to existing memories:
```python
async def enrich_symbol_with_memory(
    symbol: LSPSymbol,
    project_path: str
) -> EnrichedSymbol:
    """Find memories and entities related to this symbol."""
    memories = await memory_manager.recall_for_file(symbol.file_path)
    entities = await entity_manager.find_by_name(symbol.name)
    return EnrichedSymbol(symbol=symbol, memories=memories, entities=entities)
```

#### Integration 4: Watcher → LSP Sync

**Location:** `daem0nmcp/watcher.py` (extension)

**Pattern:** File changes trigger LSP document sync:
```python
async def on_file_change(file_path: str, change_type: str):
    # Existing: update index
    await code_indexer.invalidate(file_path)
    
    # New: notify LSP
    lsp_manager = await context.get_lsp_manager()
    await lsp_manager.notify_document_change(file_path, change_type)
```

---

## 3. IMPLEMENTATION ROADMAP

### Phase 1: Foundation (LSP Client Integration)
**Duration:** 2-3 weeks  
**Goal:** Working LSP client infrastructure with basic connectivity

| Task | Effort | Dependencies | Owner |
|------|--------|--------------|-------|
| **1.1** Add multilspy dependency | S | None | Backend |
| **1.2** Create `daem0nmcp/lsp/` package structure | S | 1.1 | Backend |
| **1.3** Implement `LSPManager` class | M | 1.2 | Backend |
| **1.4** Implement language server discovery | M | 1.3 | Backend |
| **1.5** Add LSP configuration to Settings | S | 1.3 | Backend |
| **1.6** Integrate LSPManager into ProjectContext | M | 1.4 | Backend |
| **1.7** Write integration tests | M | 1.6 | Backend |
| **1.8** Document LSP setup in README | S | 1.7 | Docs |

**Success Criteria:**
1. `LSPManager` can start/stop language servers for Python and TypeScript
2. LSP processes auto-download on first use (via multilspy)
3. Configuration via `DAEM0NMCP_LSP_ENABLED` and `DAEM0NMCP_LSP_SERVERS`
4. All tests pass; no regressions in existing functionality

**Technical Specification:**
```python
# daem0nmcp/lsp/manager.py
class LSPManager:
    """Manages LSP client lifecycle per project."""
    
    def __init__(self, project_path: str, config: LSPConfig):
        self.project_path = project_path
        self.config = config
        self._clients: Dict[str, LSPClient] = {}  # lang -> client
    
    @classmethod
    async def create(cls, project_path: str) -> "LSPManager":
        """Factory with auto language detection."""
        ...
    
    async def get_client(self, language: str) -> Optional[LSPClient]:
        """Get or create LSP client for language."""
        ...
    
    async def shutdown(self):
        """Gracefully shutdown all LSP processes."""
        ...
```

---

### Phase 2: Core LSP Features (Query Tools)
**Duration:** 3-4 weeks  
**Goal:** Essential LSP query tools available via MCP

| Task | Effort | Dependencies | Owner |
|------|--------|--------------|-------|
| **2.1** Implement `lsp_goto_definition` | M | Phase 1 | Backend |
| **2.2** Implement `lsp_find_references` | M | Phase 1 | Backend |
| **2.3** Implement `lsp_hover` | S | Phase 1 | Backend |
| **2.4** Implement `lsp_document_symbols` | M | Phase 1 | Backend |
| **2.5** Implement `lsp_workspace_symbols` | M | 2.4 | Backend |
| **2.6** Add `lsp` workflow action to `understand` | M | 2.1-2.5 | Backend |
| **2.7** Create symbol cache in Qdrant | L | 2.4 | Backend |
| **2.8** Add LSP results to `propose_refactor` | M | 2.1-2.3 | Backend |
| **2.9** Write feature tests | M | 2.1-2.8 | Backend |
| **2.10** Create usage examples | S | 2.9 | Docs |

**Success Criteria:**
1. User can call `lsp_goto_definition(file, line, col)` and get accurate results
2. `lsp_find_references` returns all references across project
3. `lsp_hover` returns type info and documentation
4. Symbol queries complete in <2 seconds for typical projects
5. Results integrate with existing `propose_refactor` output

**MCP Tools to Implement (Phase 2):**

| Tool | Description | Priority |
|------|-------------|----------|
| `lsp_goto_definition` | Navigate to symbol definition | HIGH |
| `lsp_find_references` | Find all symbol references | HIGH |
| `lsp_hover` | Get type info and docs at position | HIGH |
| `lsp_document_symbols` | List symbols in file | MEDIUM |
| `lsp_workspace_symbols` | Search symbols across project | MEDIUM |
| `lsp_prepare_rename` | Validate rename operation | MEDIUM |
| `lsp_call_hierarchy` | Show call hierarchy | LOW |
| `lsp_type_hierarchy` | Show type hierarchy | LOW |

---

### Phase 3: Advanced Features (Refactoring & Diagnostics)
**Duration:** 3-4 weeks  
**Goal:** Full refactoring support and real-time diagnostics

| Task | Effort | Dependencies | Owner |
|------|--------|--------------|-------|
| **3.1** Implement `lsp_rename_symbol` | L | Phase 2 | Backend |
| **3.2** Implement `lsp_code_action` | L | Phase 2 | Backend |
| **3.3** Implement `lsp_format_document` | M | Phase 2 | Backend |
| **3.4** Implement `lsp_code_lens` | M | Phase 2 | Backend |
| **3.5** Add diagnostics collection | L | Phase 1 | Backend |
| **3.6** Create diagnostics history in SQLite | M | 3.5 | Backend |
| **3.7** Integrate diagnostics with file watcher | M | 3.5 | Backend |
| **3.8** Add `lsp_get_diagnostics` tool | S | 3.5 | Backend |
| **3.9** Create refactoring safety checks | L | 3.1, 2.8 | Backend |
| **3.10** Write comprehensive tests | L | 3.1-3.9 | Backend |

**Success Criteria:**
1. User can rename symbols with LSP-validated edits
2. Diagnostics appear within 5 seconds of code changes
3. Code actions (quick fixes) available via MCP
4. Refactoring checks for conflicts with memory constraints
5. All features work for Python, TypeScript, and Rust

**MCP Tools to Implement (Phase 3):**

| Tool | Description | Priority |
|------|-------------|----------|
| `lsp_rename_symbol` | Rename symbol across project | HIGH |
| `lsp_code_action` | Execute code actions | HIGH |
| `lsp_format_document` | Format code | MEDIUM |
| `lsp_format_range` | Format selection | MEDIUM |
| `lsp_code_lens` | Get code lens hints | LOW |
| `lsp_get_diagnostics` | Fetch errors/warnings | HIGH |
| `lsp_execute_command` | Run LSP commands | LOW |

---

### Phase 4: Daem0n-Specific Enhancements (Memory-Aware LSP)
**Duration:** 2-3 weeks  
**Goal:** Differentiated features leveraging persistent memory

| Task | Effort | Dependencies | Owner |
|------|--------|--------------|-------|
| **4.1** Link LSP symbols to memory graph | L | Phase 2 | Backend |
| **4.2** Implement `lsp_find_with_context` | M | 4.1 | Backend |
| **4.3** Add "why was this written" to hover | M | 4.1 | Backend |
| **4.4** Create symbol evolution tracking | L | 4.1 | Backend |
| **4.5** Implement memory-aware rename safety | M | 3.1, 4.1 | Backend |
| **4.6** Add symbol-related memories to results | M | 4.1 | Backend |
| **4.7** Create `lsp_explain_symbol` tool | M | 4.2-4.4 | Backend |
| **4.8** Integrate with dreaming for symbol insights | L | 4.1-4.7 | Backend |
| **4.9** Write advanced feature tests | M | 4.1-4.8 | Backend |
| **4.10** Create differentiation showcase | S | 4.9 | Docs |

**Success Criteria:**
1. LSP results include related memories and decisions
2. `lsp_explain_symbol` shows history, decisions, and evolution
3. Rename operations warn about conflicting past decisions
4. Users can ask "why" about any symbol and get contextual answer
5. Demonstrable 3+ features that Serena cannot replicate

**Unique Tools (Phase 4 - Daem0n Differentiation):**

| Tool | Description | Uniqueness |
|------|-------------|------------|
| `lsp_explain_symbol` | Explain symbol with memory context | Links to decisions, history |
| `lsp_find_with_context` | Find symbols + project context | Includes causal chains |
| `lsp_trace_symbol_evolution` | Show how symbol changed over time | Temporal analysis |
| `lsp_suggest_with_memory` | Suggest completions from patterns | Learns from past edits |
| `lsp_smart_rename` | Rename with conflict detection | Checks memory for constraints |

---

### Roadmap Summary

| Phase | Duration | Deliverables | Cumulative Tools |
|-------|----------|--------------|------------------|
| **1: Foundation** | 2-3 weeks | LSP client infrastructure | 0 (infrastructure) |
| **2: Core** | 3-4 weeks | 8 query tools | 8 |
| **3: Advanced** | 3-4 weeks | 7 refactoring/diagnostic tools | 15 |
| **4: Memory-Aware** | 2-3 weeks | 5 differentiated tools | 20 |
| **Total** | **10-14 weeks** | **Full LSP integration** | **20 tools** |

---

## 4. COMPETITIVE DIFFERENTIATION

### 4.1 Daem0nMCP + LSP vs Serena

| Dimension | Serena | Daem0nMCP + LSP |
|-----------|--------|-----------------|
| **LSP Tools** | 50+ static tools | 20 intelligent tools |
| **Symbol Context** | Static analysis only | Symbol + project memory |
| **Historical Understanding** | ❌ None | ✅ Full decision history |
| **Temporal Analysis** | ❌ None | ✅ `trace_symbol_evolution` |
| **Cross-project** | ❌ Per-session only | ✅ Federated memory |
| **Refactoring Safety** | LSP-provided only | LSP + memory constraints |
| **Learning** | ❌ None | ✅ Learns from past edits |
| **Offline Capability** | ❌ Requires LSP servers | ✅ Graceful degradation |

### 4.2 Unique Value Propositions

#### UVP 1: "Code with Memory"
**Tagline:** *"Every symbol tells a story"*

Serena can tell you WHAT a function does. Daem0nMCP+LSP can tell you:
- What it does (LSP)
- WHY it was written that way (memory decisions)
- WHO wrote it and when (entity linking)
- HOW it's evolved (temporal tracking)
- WHAT to watch out for (warning memories)

**Example:**
```
User: Explain the UserService class

Serena: "UserService has methods: createUser, updateUser, deleteUser..."

Daem0nMCP+LSP: "UserService class handles user lifecycle. 
  ⚠️ WARNING: Previous refactor attempt failed (see decision #1847)
  📚 DECISION: Uses repository pattern (decided 2025-08-15)
  🔗 RELATED: UserEntity, AuthService, DatabaseManager
  📈 EVOLUTION: Originally 200 LOC, refactored to 80 LOC on 2025-09-01"
```

#### UVP 2: "Safe Refactoring"
**Tagline:** *"Learn from past mistakes"*

Before applying a rename or refactor, Daem0nMCP checks:
- LSP validation (will it break?)
- Memory constraints (have we tried this before?)
- Decision history (is this against established patterns?)
- Warning memories (are there known issues?)

**Example:**
```
User: Rename 'processData' to 'transformData'

Daem0nMCP+LSP: "⚠️ BLOCKED: Previous rename attempt failed
  (see decision #1923: caused 12 test failures)
  SUGGESTION: Use 'normalizeData' instead (similar intent, no conflicts)"
```

#### UVP 3: "Continuous Code Intelligence"
**Tagline:** *"Your codebase, always learning"*

Serena is stateless. Daem0nMCP:
- Remembers every decision
- Connects related symbols automatically
- Dreams about code relationships during idle
- Improves suggestions over time

### 4.3 Recommended Positioning

**Primary Positioning:**
> "Daem0nMCP is the only code intelligence tool that combines LSP's semantic understanding with persistent project memory. While other tools tell you what your code does, Daem0nMCP explains why it's written that way and helps you avoid past mistakes."

**Secondary Positioning (for LSP specifically):**
> "Daem0nMCP's LSP integration doesn't just provide go-to-definition and refactoring—it enriches every interaction with your project's history, decisions, and learned patterns. It's like having a senior engineer who's been on the project since day one."

**Target Use Cases:**
1. **Legacy Code Understanding:** "Why does this weird code exist?"
2. **Refactoring Confidence:** "Is it safe to change this?"
3. **Onboarding Acceleration:** "What should I know about this module?"
4. **Knowledge Preservation:** "Document decisions where they matter"

---

## 5. TECHNICAL SPECIFICATION

### 5.1 File Structure

```
daem0nmcp/
├── lsp/
│   ├── __init__.py              # Package exports
│   ├── manager.py               # LSPManager class
│   ├── client.py                # LSPClient wrapper
│   ├── config.py                # LSPConfiguration
│   ├── features/
│   │   ├── __init__.py
│   │   ├── navigation.py        # goto_definition, find_references
│   │   ├── symbols.py           # document_symbols, workspace_symbols
│   │   ├── editing.py           # rename, code_action
│   │   ├── diagnostics.py       # get_diagnostics
│   │   └── memory_aware.py      # explain_symbol, find_with_context
│   ├── models.py                # LSP dataclasses
│   └── bridge.py                # LSP <-> Memory integration
├── tools/
│   ├── ...                      # Existing tools
│   └── lsp_tools.py             # MCP tool wrappers (20 tools)
├── workflows/
│   ├── ...                      # Existing workflows
│   └── understand.py            # Add 'lsp' action
└── ...
```

### 5.2 Key Classes/Interfaces

#### LSPManager
```python
# daem0nmcp/lsp/manager.py

from typing import Dict, Optional
from dataclasses import dataclass

@dataclass
class LSPConfig:
    enabled: bool = True
    auto_download: bool = True
    server_configs: Dict[str, Dict] = None  # per-language config
    timeout_seconds: int = 30

class LSPManager:
    """
    Manages LSP client lifecycle per project.
    One LSPManager per ProjectContext.
    """
    
    def __init__(self, project_path: str, config: LSPConfig):
        self.project_path = project_path
        self.config = config
        self._clients: Dict[str, 'LSPClient'] = {}
        self._initialized = False
    
    @classmethod
    async def create(cls, project_path: str, config: Optional[LSPConfig] = None) -> "LSPManager":
        """
        Factory method with auto language detection.
        Scans project to determine which LSP servers to start.
        """
        ...
    
    async def initialize(self):
        """Start LSP clients for detected languages."""
        ...
    
    async def get_client(self, language: str) -> Optional['LSPClient']:
        """Get or lazily initialize LSP client for language."""
        ...
    
    async def get_client_for_file(self, file_path: str) -> Optional['LSPClient']:
        """Get appropriate LSP client based on file extension."""
        ...
    
    async def shutdown(self):
        """Gracefully shutdown all LSP processes."""
        ...
    
    async def notify_document_change(self, file_path: str, change_type: str):
        """Notify relevant LSP client of file changes."""
        ...
```

#### LSPClient (multilspy wrapper)
```python
# daem0nmcp/lsp/client.py

from multilspy import LanguageServer
from typing import List, Optional, Dict, Any

class LSPClient:
    """
    Async wrapper around multilspy LanguageServer.
    Provides Daem0nMCP-specific convenience methods.
    """
    
    def __init__(self, language: str, project_path: str, config: Dict):
        self.language = language
        self.project_path = project_path
        self._server: Optional[LanguageServer] = None
    
    async def initialize(self):
        """Start the language server process."""
        ...
    
    async def request_definition(
        self, 
        file_path: str, 
        line: int, 
        column: int
    ) -> List[Dict[str, Any]]:
        """LSP textDocument/definition"""
        ...
    
    async def request_references(
        self,
        file_path: str,
        line: int,
        column: int,
        include_declaration: bool = True
    ) -> List[Dict[str, Any]]:
        """LSP textDocument/references"""
        ...
    
    async def request_hover(
        self,
        file_path: str,
        line: int,
        column: int
    ) -> Optional[Dict[str, Any]]:
        """LSP textDocument/hover"""
        ...
    
    async def request_document_symbols(
        self,
        file_path: str
    ) -> List[Dict[str, Any]]:
        """LSP textDocument/documentSymbol"""
        ...
    
    async def request_rename(
        self,
        file_path: str,
        line: int,
        column: int,
        new_name: str
    ) -> Optional[Dict[str, Any]]:
        """LSP textDocument/rename"""
        ...
    
    async def shutdown(self):
        """Shutdown language server."""
        ...
```

#### SymbolBridge (Memory Integration)
```python
# daem0nmcp/lsp/bridge.py

from typing import List, Dict, Any, Optional
from dataclasses import dataclass

@dataclass
class EnrichedSymbol:
    """LSP symbol enhanced with Daem0nMCP memory context."""
    name: str
    kind: str
    location: Dict[str, Any]
    
    # LSP-provided
    lsp_data: Dict[str, Any]
    
    # Daem0nMCP-enriched
    memories: List[Dict[str, Any]]
    entities: List[Dict[str, Any]]
    decisions: List[Dict[str, Any]]
    warnings: List[Dict[str, Any]]
    evolution_history: List[Dict[str, Any]]

class SymbolBridge:
    """
    Bridges LSP symbols with Daem0nMCP memory system.
    """
    
    def __init__(
        self,
        memory_manager: 'MemoryManager',
        entity_manager: 'EntityManager',
        project_path: str
    ):
        self.memory_manager = memory_manager
        self.entity_manager = entity_manager
        self.project_path = project_path
    
    async def enrich_symbol(
        self,
        symbol: Dict[str, Any],
        include_memories: bool = True,
        include_history: bool = True
    ) -> EnrichedSymbol:
        """
        Enrich LSP symbol with project memory context.
        """
        ...
    
    async def find_symbol_memories(
        self,
        symbol_name: str,
        file_path: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Find memories related to a symbol."""
        ...
    
    async def trace_symbol_evolution(
        self,
        symbol_name: str,
        file_path: str
    ) -> List[Dict[str, Any]]:
        """Trace how a symbol has changed over time."""
        ...
    
    async def check_refactoring_safety(
        self,
        symbol_name: str,
        operation: str  # 'rename', 'move', 'delete'
    ) -> Dict[str, Any]:
        """
        Check if refactoring is safe based on memory constraints.
        Returns warnings and suggestions.
        """
        ...
```

### 5.3 Configuration Schema

Add to `daem0nmcp/config.py`:

```python
class Settings(BaseSettings):
    # ... existing settings ...
    
    # LSP Configuration
    lsp_enabled: bool = True
    lsp_auto_download: bool = True  # Auto-download language servers
    lsp_timeout_seconds: int = 30
    lsp_cache_symbols: bool = True  # Cache symbols in Qdrant
    lsp_cache_ttl_seconds: int = 3600
    
    # Per-language server configurations
    # Format: {language: {command: [...], settings: {...}}}
    lsp_server_configs: Dict[str, Dict] = Field(default_factory=dict)
    
    # Default configs for common languages
    lsp_default_python: str = "pyright"  # or "pylsp", "jedi"
    lsp_default_typescript: str = "typescript-language-server"
    lsp_default_rust: str = "rust-analyzer"
    lsp_default_go: str = "gopls"
    
    # Symbol enrichment
    lsp_enrich_with_memories: bool = True
    lsp_max_memories_per_symbol: int = 5
    lsp_include_decisions: bool = True
    lsp_include_warnings: bool = True
```

**Environment Variables:**
```bash
# Enable/disable LSP
DAEM0NMCP_LSP_ENABLED=true

# Auto-download language servers
DAEM0NMCP_LSP_AUTO_DOWNLOAD=true

# Timeout for LSP operations
DAEM0NMCP_LSP_TIMEOUT_SECONDS=30

# Cache symbols for faster queries
DAEM0NMCP_LSP_CACHE_SYMBOLS=true

# Include memory context in LSP results
DAEM0NMCP_LSP_ENRICH_WITH_MEMORIES=true
```

### 5.4 MCP Tools to Implement

#### Phase 2: Core Query Tools

```python
# daem0nmcp/tools/lsp_tools.py

@mcp.tool(version=__version__)
@with_request_id
async def lsp_goto_definition(
    file_path: str,
    line: int,
    column: int,
    project_path: Optional[str] = None
) -> Dict[str, Any]:
    """
    Navigate to the definition of the symbol at the given position.
    
    Returns the location(s) where the symbol is defined, including
    file path, line, and column information.
    """

@mcp.tool(version=__version__)
@with_request_id
async def lsp_find_references(
    file_path: str,
    line: int,
    column: int,
    include_declaration: bool = True,
    project_path: Optional[str] = None
) -> Dict[str, Any]:
    """
    Find all references to the symbol at the given position.
    
    Returns a list of locations where the symbol is referenced
    across the entire project.
    """

@mcp.tool(version=__version__)
@with_request_id
async def lsp_hover(
    file_path: str,
    line: int,
    column: int,
    project_path: Optional[str] = None
) -> Dict[str, Any]:
    """
    Get hover information (type, documentation) at the given position.
    
    Returns type information, signature, and documentation
    for the symbol under the cursor.
    """

@mcp.tool(version=__version__)
@with_request_id
async def lsp_document_symbols(
    file_path: str,
    project_path: Optional[str] = None
) -> Dict[str, Any]:
    """
    Get all symbols defined in a document.
    
    Returns classes, functions, variables, and other symbols
    defined in the specified file.
    """

@mcp.tool(version=__version__)
@with_request_id
async def lsp_workspace_symbols(
    query: str,
    limit: int = 20,
    project_path: Optional[str] = None
) -> Dict[str, Any]:
    """
    Search for symbols across the entire workspace.
    
    Query matches against symbol names using fuzzy matching.
    """
```

#### Phase 3: Refactoring & Diagnostics

```python
@mcp.tool(version=__version__)
@with_request_id
async def lsp_rename_symbol(
    file_path: str,
    line: int,
    column: int,
    new_name: str,
    dry_run: bool = False,
    project_path: Optional[str] = None
) -> Dict[str, Any]:
    """
    Rename a symbol and all its references.
    
    If dry_run is True, returns the changes without applying them.
    Includes safety checks against project memory.
    """

@mcp.tool(version=__version__)
@with_request_id
async def lsp_code_action(
    file_path: str,
    line: int,
    column: int,
    only_kinds: Optional[List[str]] = None,
    project_path: Optional[str] = None
) -> Dict[str, Any]:
    """
    Get available code actions (quick fixes) at the given position.
    
    Returns actions like "Add import", "Fix typo", "Extract method", etc.
    """

@mcp.tool(version=__version__)
@with_request_id
async def lsp_format_document(
    file_path: str,
    options: Optional[Dict[str, Any]] = None,
    project_path: Optional[str] = None
) -> Dict[str, Any]:
    """
    Format the entire document using the language server.
    
    Returns the formatted text or applied changes.
    """

@mcp.tool(version=__version__)
@with_request_id
async def lsp_get_diagnostics(
    file_path: Optional[str] = None,
    include_history: bool = False,
    project_path: Optional[str] = None
) -> Dict[str, Any]:
    """
    Get diagnostics (errors, warnings) from the language server.
    
    If file_path is None, returns diagnostics for all files.
    Can include historical diagnostic trends.
    """
```

#### Phase 4: Memory-Aware Tools (Differentiation)

```python
@mcp.tool(version=__version__)
@with_request_id
async def lsp_explain_symbol(
    file_path: str,
    line: int,
    column: int,
    include_memories: bool = True,
    include_evolution: bool = True,
    project_path: Optional[str] = None
) -> Dict[str, Any]:
    """
    Get a comprehensive explanation of a symbol enriched with project memory.
    
    Combines LSP information (type, docs) with:
    - Why the symbol was created (decisions)
    - How it's evolved over time
    - Related warnings or constraints
    - Connected entities and memories
    
    This is Daem0nMCP's unique "explain with context" feature.
    """

@mcp.tool(version=__version__)
@with_request_id
async def lsp_find_with_context(
    query: str,
    include_memories: bool = True,
    include_decisions: bool = True,
    limit: int = 10,
    project_path: Optional[str] = None
) -> Dict[str, Any]:
    """
    Search for symbols with their project context.
    
    Like workspace_symbols but enriches results with:
    - Memories about each symbol
    - Decisions that influenced the symbol
    - Related files and entities
    """

@mcp.tool(version=__version__)
@with_request_id
async def lsp_trace_symbol_evolution(
    symbol_name: str,
    file_path: str,
    max_history: int = 10,
    project_path: Optional[str] = None
) -> Dict[str, Any]:
    """
    Trace how a symbol has changed over time.
    
    Analyzes git history and memory to show:
    - When the symbol was created
    - Significant changes and their authors
    - Decisions that led to changes
    - Current state vs historical states
    """

@mcp.tool(version=__version__)
@with_request_id
async def lsp_smart_rename(
    file_path: str,
    line: int,
    column: int,
    new_name: str,
    project_path: Optional[str] = None
) -> Dict[str, Any]:
    """
    Rename a symbol with memory-aware safety checks.
    
    Before renaming, checks:
    - Standard LSP validation
    - Past failed rename attempts
    - Naming conventions from decisions
    - Potential conflicts with warnings
    
    Provides confidence score and suggestions.
    """
```

### 5.5 Integration with Existing `understand` Workflow

The `understand` workflow will get a new `lsp` action:

```python
# daem0nmcp/workflows/understand.py

async def dispatch(action: str, ...):
    if action == "lsp":
        return await handle_lsp_action(**kwargs)
    # ... existing actions ...

async def handle_lsp_action(
    subaction: str,  # 'definition', 'references', 'hover', etc.
    **kwargs
):
    """
    LSP-related code understanding actions.
    
    Subactions:
    - definition: goto definition
    - references: find references  
    - hover: get type/docs
    - symbols: document/workspace symbols
    - explain: enriched explanation with memory
    """
    # Delegate to appropriate LSP tool
    ...
```

---

## 6. SUCCESS METRICS

### Phase Completion Criteria

| Phase | Metric | Target |
|-------|--------|--------|
| **1** | LSP servers start for Python/TS | 100% success rate |
| **1** | Auto-download success | 95% success rate |
| **2** | Definition accuracy | >90% correct |
| **2** | Query latency (p95) | <2 seconds |
| **3** | Rename success rate | >95% no regressions |
| **3** | Diagnostics latency | <5 seconds |
| **4** | Memory enrichment coverage | >80% symbols have context |
| **4** | User satisfaction (NPS) | >50 |

### Adoption Metrics

- **Tool Usage:** LSP tools represent 20%+ of total tool calls within 3 months
- **Language Coverage:** Support for 5+ languages actively used
- **Error Reduction:** 30% fewer refactoring-related bugs (measured via outcomes)
- **Efficiency:** 25% reduction in "explain this code" time

---

## 7. CONCLUSION & RECOMMENDATIONS

### Summary

Integrating LSP functionality into Daem0nMCP is **technically feasible, architecturally compatible, and strategically valuable**. The Direct LSP Client approach via multilspy minimizes risk while maximizing language coverage.

**Key Advantages:**
1. **Differentiation:** Memory-aware LSP tools create unique value vs Serena
2. **Synergy:** Enhances existing tree-sitter indexing, not replaces it
3. **Scalability:** Supports 100+ languages with minimal maintenance
4. **Integration:** Fits naturally into existing 6-layer architecture

### Recommendations

1. **APPROVE Phase 1** immediately to validate technical approach
2. **STAFF** 1 senior backend engineer full-time for 10-14 weeks
3. **PARALLEL TRACK:** Begin design of Phase 4 differentiation features while Phase 2/3 in progress
4. **BETA PROGRAM:** Identify 3-5 power users for early Phase 2 feedback
5. **MEASURE:** Implement metrics collection from Day 1 to validate adoption

### Risk Mitigation

- **Technical Risk:** LOW - mitigated by proven multilspy library
- **Schedule Risk:** MEDIUM - add 20% buffer to estimates
- **Adoption Risk:** LOW - enhances existing workflow, doesn't replace

---

## Appendices

### A. Language Server Support Matrix

| Language | Server | Auto-Download | Priority |
|----------|--------|---------------|----------|
| Python | pyright | ✅ Yes | P0 |
| TypeScript | typescript-language-server | ✅ Yes | P0 |
| JavaScript | typescript-language-server | ✅ Yes | P0 |
| Rust | rust-analyzer | ✅ Yes | P1 |
| Go | gopls | ✅ Yes | P1 |
| Java | jdtls | ⚠️ Manual | P2 |
| C/C++ | clangd | ⚠️ Manual | P2 |
| Ruby | solargraph | ✅ Yes | P2 |
| PHP | intelephense | ✅ Yes | P2 |
| Swift | sourcekit-lsp | ⚠️ Manual | P3 |

### B. Dependencies

```toml
# pyproject.toml additions

[dependencies]
# LSP client
"multilspy" = "^0.1.0"

# Enhanced JSON-RPC for LSP
"jsonrpc-async" = "^1.1.0"

# Optional: Language server management
"pylspclient" = { version = "^0.1.0", optional = true }
```

### C. Testing Strategy

1. **Unit Tests:** Mock LSP server responses
2. **Integration Tests:** Real pyright/tsserver in CI
3. **Performance Tests:** Large codebase benchmarks
4. **Memory Tests:** Verify no leaks in long-running LSP processes

### D. Documentation Requirements

- Setup guide for language servers
- Tool reference with examples
- Troubleshooting guide
- Migration guide for Serena users (if applicable)

---

*End of Document*
