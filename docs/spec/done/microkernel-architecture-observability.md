# Microkernel 架構強化建議（第三輪：內省能力與擴充面收尾）

> 範圍聲明：本文件僅討論**單體部署（in-process）下的 Microkernel 架構純度**，不涉及將 plugin 拆成獨立部署的微服務、跨服務通訊（gRPC/MQ）、Service Mesh 等議題。延續 [`docs/spec/done/microkernel-architecture-improvements.md`](../done/microkernel-architecture-improvements.md)（3.1–3.14）與 [`docs/spec/done/microkernel-architecture-refinements.md`](../done/microkernel-architecture-refinements.md)（4.1–4.10）的方向。

## 0. 背景與誠實的現況評估

前兩輪文件列出的 24 個項目（3.1–3.14、4.1–4.10）已全部實作完成並通過全面測試（最終 95 個測試通過）。目前系統已經是一個相當完整的 Microkernel 實作：動態發現、依賴排序、失敗隔離、能力註冊（`service_registry`）、事件匯流排（`event_bus`）、契約版本化、熱插拔、facade 抽象與自動掛載、架構邊界的自動化檢查（`pytest` gate）、scaffold 工具——這些都已經到位並有測試覆蓋。

重新檢視程式碼後，**這一輪能找到的落差明顯比前兩輪少、也更邊緣**。以下列出的都是實際驗證過（不是猜測）的具體落差，但誠實地說，它們屬於「錦上添花的觀察性/擴充性改善」，不是「違反 Microkernel 核心理念」的結構性問題——如果團隊評估後覺得現況已經足夠好，選擇不做也是合理的決定。這與前兩輪「修正明確的自我矛盾/不一致」性質不同，請據此調整期待。

## 1. 落差總覽

| 項目 | 現況 | 落差程度 |
|---|---|---|
| `ServiceRegistry`／`EventBus` 缺少內省能力 | `GET /api/v1/health` 曝露了 plugin 狀態與 facade 名單，但完全看不到「目前有哪些能力被 `service_registry` 發布」「`event_bus` 上有哪些事件被訂閱」——這兩個最核心的跨模組協作機制反而是黑箱 | 🟡 中低 |
| Plugin 沒有貢獻 middleware／exception handler 的擴充點 | `AbstractPlugin.register()` 只能加路由；`app.add_middleware()`／`app.add_exception_handler()` 只在 `main.py` 呼叫過，plugin 完全沒有管道貢獻自己的中介層或例外處理器 | 🟡 中低 |
| 只有一個真實 facade，多 facade 情境從未端到端驗證 | `facade_registry`／自動掛載機制（4.3）已有單元測試用假的 facade 驗證過 N>1 情境，但整個 app 裡實際存在的 facade 仍然只有 `CatalogFacade` 一個，沒有一個「兩個真實 facade 同時運作」的端到端案例 | 🟢 低 |
| Plugin 沒有獨立於 `api_version` 的自身版本號 | `api_version` 的用途是「相容於哪個 kernel 契約版本」，不是「這個 plugin 自己發布到第幾版」；`/health` 沒有地方能看出「目前跑的 `product_plugin` 是哪個版本」 | 🟢 低 |
| 治理缺口延續（非新發現，重申既有已知限制） | `ruff.toml` 仍是既有的格式錯誤（`[tool.ruff]` 應為 `[lint]`），專案仍非 git repository，因此仍然沒有真正的 CI pipeline，`scripts/check_architecture_boundaries.py` 仍只能靠 `pytest` 執行 | 🟢 低 |

## 2. 詳細改善項目

### 5.1 `ServiceRegistry`／`EventBus` 缺少內省能力

**現況問題**：驗證過整個 `app/` 目錄，沒有任何地方呼叫 `service_registry.names()`；`EventBus` 甚至連一個列舉方法都沒有（`app/core/hooks.py` 只有 `subscribe`/`unsubscribe`/`unsubscribe_all_from`/`emit`/`clear`，沒有任何 `names()`/`subscriptions()` 之類的唯讀方法）。

**為何是落差**：`/health` 端點在前兩輪已經逐步補齊「plugin 狀態」「facade 名單」，讓維運者能一眼看出這個 process 裡有什麼——但兩個最關鍵的跨模組協作原語（「誰發布了什麼能力」「誰在聽什麼事件」）反而完全看不到。當一個 plugin 因為 `resolve()` 失敗而在某個請求中回傳 `Result.Err`（例如 facade 呼叫 `service_registry.resolve("product_service_factory")` 但 `product_plugin` 被停用了），維運者除了看 log，沒有任何 API 可以主動確認「這個能力到底有沒有被發布」。

**建議做法**：
- `EventBus` 增加一個唯讀的 `subscriptions() -> dict[str, list[str | None]]`（事件名稱 → owner 名單），比照 `ServiceRegistry.names()` 的精神。
- `GET /api/v1/health` 增加兩個欄位：`capabilities: list[str]`（`service_registry.names()`）與 `event_subscriptions: dict[str, list[str]]`（`event_bus.subscriptions()`，owner 為 `None` 的訂閱可以標記成 `"unknown"` 或直接濾掉，畢竟目前所有透過 `ctx.event_bus.subscribe()` 訂閱的都會有 owner）。

**影響檔案**：`app/core/hooks.py`（新增 `subscriptions()`）、`app/api/v1/health.py`（新增欄位）、`docs/technical/operations.md`（更新範例回應）。

### 5.2 Plugin 沒有貢獻 middleware／exception handler 的擴充點

**現況問題**：`app/main.py` 的 `create_app()` 直接呼叫 `app.add_middleware(GlobalExceptionMiddleware)`、`app.add_middleware(CORSMiddleware, ...)`、`app.add_exception_handler(RateLimitExceeded, ...)`。`AbstractPlugin.register(app, ctx)` 雖然收到了 `app: FastAPI`，理論上可以直接呼叫 `app.add_middleware(...)`，但這件事完全沒有被文件化、也沒有任何示範，實務上沒有 plugin 這樣做過。

**為何是落差**：Microkernel 的 plugin 應該能獨立擴充 kernel 的行為，而不只是「加路由」。如果某個 plugin 想要有自己的例外類型（例如 `BillingLimitExceeded`）並且想要一個全域的例外處理器把它轉成特定格式的回應，目前沒有建議的做法——硬要做的話，plugin 可以在 `register()` 裡直接呼叫 `app.add_exception_handler(...)`，但 FastAPI 的 middleware/exception handler 註冊有**順序敏感性**（例如 middleware 是後註冊、先執行），多個 plugin 各自呼叫 `app.add_middleware()` 卻不知道彼此的存在，可能會踩到互相覆蓋或順序錯亂的坑，而目前完全沒有防呆或文件提醒。

**建議做法**：
- 短期（文件層級）：在 [plugin-development.md](../../technical/plugin-development.md) 明確寫下「plugin 可以在 `register()` 呼叫 `app.add_exception_handler(YourError, your_handler)` 來處理自己領域的例外類型」，並提醒 middleware 的順序風險（一般不建議 plugin 加全域 middleware，僅建議 exception handler，因為 exception handler 是按例外類型 dispatch、天生沒有順序衝突問題）。
- 不建議做的事：不要在 `AbstractPlugin` 契約裡新增專門的 `middlewares`/`exception_handlers` 宣告式介面——這對目前只有兩個範例 plugin、都不需要自訂例外類型的專案來說是過度設計；等真的有 plugin 需要時，用文件建議的「直接呼叫 `app.add_exception_handler`」已經夠用。

**影響檔案**：`docs/technical/plugin-development.md`（新增一小節，純文件）。

### 5.3 只有一個真實 facade，多 facade 情境從未端到端驗證

**現況問題**：4.3 的自動掛載機制（`facade_registry.routers()`）已經用**假的**（synthetic）`AbstractFacade` 子類別在單元測試中驗證過「可以登記多個」「router 可選」，但整個 `app/` 目錄裡，實際存在、有真實業務邏輯與路由的 facade 仍然只有 `CatalogFacade` 一個。

**為何是落差**：單元測試證明了機制「理論上」支援多個 facade，但沒有端到端證明——例如兩個 facade 是否可能在 `main.py` 掛載時發生路由前綴衝突、`facade_registry.register()` 的重複名稱檢查是否在真實情境下也一樣有效、Swagger UI 的 OpenAPI 文件在有多個 facade 時分類（tags）是否清楚。

**建議做法**：這是最適合「等真的需要第二個 facade 時再驗證」的一種落差——不建議現在為了驗證而刻意生造一個沒有實際商業意義的第二個 facade（那會是為了測試而測試，反而增加無意義的維護負擔）。若團隊近期會新增涉及跨 plugin 協調的功能，屆時新增第二個 facade 就會自然驗證這個情境；本項目的建議是**列為已知限制、暫不主動處理**。

**影響檔案**：無（記錄性質，等待自然發生的時機）。

### 5.4 Plugin 沒有獨立於 `api_version` 的自身版本號

**現況問題**：`AbstractPlugin.api_version` 的語意是「這個 plugin 相容於哪個 kernel 契約版本」（見 `docs/spec/done/microkernel-architecture-improvements.md` S3.7），不是「這個 plugin 自己的發布版本」。目前沒有欄位可以回答「現在跑的 `product_plugin` 原始碼是哪個版本/哪次修改」。

**為何是落差**：對於單一 repo、單一部署的模板專案來說，這個資訊通常可以直接從 git commit/tag 取得，價值有限；但如果未來這個 microkernel 樣板被多個團隊共用、plugin 可能來自不同的發布節奏（例如 `product_plugin` 是團隊 A 維護、獨立發版），`/health` 能顯示「現在跑的是哪個版本」會對事故排查很有幫助。

**建議做法**：若要做，`AbstractPlugin` 增加一個可選的 `version: ClassVar[str] = "0.0.0"`（純資訊性，不參與任何相容性判斷，純粹是"顯示用"，避免與 `api_version` 的相容性語意混淆），`/health` 的 `PluginHealth` 增加 `version` 欄位。**優先度很低**：目前兩個範例 plugin 都沒有獨立版本發布的需求，這是「有多團隊協作、多發版節奏時才用得到」的功能，建議先不做，等真的有這個需求再加。

**影響檔案**：`app/core/plugin_base.py`、`app/api/v1/health.py`（若要做）。

### 5.5 治理缺口延續（非新發現）

**現況問題**：`ruff.toml` 仍然是前兩輪就發現、但明確標記「非本次任務範圍」的既有格式錯誤（`[tool.ruff]` 應為獨立 `ruff.toml` 的 `[lint]`），導致 `ruff check` 無法執行；專案仍然不是 git repository，因此 `scripts/check_architecture_boundaries.py` 仍然只能靠 `pytest` 執行，沒有真正在 PR 階段擋下違規的 CI。

**為何重提**：這兩項從第一輪文件就存在，一直被標記為「觀察但不修正」，因為前者是 lint 設定問題（與架構無關）、後者需要使用者決定是否要初始化版本控制（屬於基礎設施決策，不是單方面該由架構文件驅動的事）。這裡重新列出只是為了讓「已知限制清單」保持在同一個地方，不需要另外去翻前兩份文件才知道這些還沒解決。

**建議做法**：維持現狀，不在本輪處理。若使用者未來決定要初始化 git 並接上 GitHub，屆時再一併處理 `ruff.toml` 與新增 CI workflow 會更有效率（兩者都跟「這個 repo 開始被版本控制」這個事件綁在一起）。

**影響檔案**：無（記錄性質）。

## 3. 建議導入順序

| 階段 | 項目 | 理由 |
|---|---|---|
| 建議做（低成本、有實際觀察性價值） | 5.1 `ServiceRegistry`/`EventBus` 內省能力、5.2 Plugin exception handler 擴充點文件化 | 兩項都是小改動（5.1 是加兩個唯讀方法+兩個回應欄位；5.2 純文件），且直接補上目前唯一還看不到的「能力/事件」黑箱，觀察性價值高於成本。 |
| 建議暫緩，等自然時機 | 5.3 多 facade 端到端驗證 | 不該為了驗證而生造第二個 facade；等真的有第二個跨 plugin 協調需求時自然驗證。 |
| 建議暫緩，需求不明確 | 5.4 Plugin 自身版本號 | 目前沒有多團隊/多發版節奏的實際需求，先不做，避免過度設計。 |
| 維持現狀 | 5.5 `ruff.toml`／git repo 治理缺口 | 屬於基礎設施決策，不是架構文件該單方面驅動的事，待使用者決定初始化版本控制時一併處理。 |

## 3.5 實作 Checklist（優先度「中低」項目：5.1–5.2）

> 狀態：依使用者指示，只執行落差總覽表中標記 🟡（中低）的 2 個項目（5.1、5.2）。完成後執行 `pytest -q` → **98 passed**，重跑確認穩定、邊界檢查通過、無殘留檔案。

- [x] **5.1 `ServiceRegistry`/`EventBus` 內省能力**：`app/core/hooks.py` 的 `EventBus` 新增 `subscriptions() -> dict[str, list[str]]`（事件名稱 → owner 名單，無 owner 的訂閱顯示為 `"<unowned>"`，無訂閱者的事件不列出）。`app/api/v1/health.py` 的 `HealthResponse` 新增 `capabilities: list[str]`（`service_registry.names()`）與 `event_subscriptions: dict[str, list[str]]`（`event_bus.subscriptions()`）兩個欄位。新增 3 個 `EventBus.subscriptions()` 單元測試（owner 回報、無訂閱者時略過、`unsubscribe_all_from` 後正確反映），並更新 `tests/test_api.py::test_health_check` 驗證真實回應包含 `product_service_factory`/`user_service_factory` 與 `user_plugin` 對 `product.created` 的訂閱。同步更新 `docs/technical/operations.md` 的範例回應與說明。
- [x] **5.2 文件化 Plugin exception handler 擴充點**：`docs/technical/plugin-development.md` 新增「Contributing an exception handler (optional)」一節，示範 plugin 如何在 `register()` 用 `app.add_exception_handler(YourError, handler)` 處理自己領域的例外類型（安全，因為 FastAPI 依例外類型 dispatch、與註冊順序無關），並明確說明**不建議**同樣的方式用在 `app.add_middleware()`（順序敏感，多 plugin 各自加會有互相覆蓋/順序錯亂風險），改用該 plugin 自己路由上的 `Depends(...)`，或需要全域行為時直接寫在 `main.py`。純文件，未修改 `AbstractPlugin` 契約或任何程式碼行為（維持原文件建議：不新增宣告式的 `middlewares`/`exception_handlers` 介面，避免過度設計）。

## 3.6 實作 Checklist（優先度「低」項目：5.3–5.5）

> 狀態：本文件原本對 5.3–5.5 的建議是「暫緩／維持現狀」，並非「待辦」。經使用者明確要求並逐項確認後才動手，完成後執行 `pytest -q` → **99 passed**，重跑確認穩定、邊界檢查通過、`ruff check` 可正常執行、無殘留檔案。

- [x] **5.3 一次性驗證多 facade 端到端運作**：建立臨時的第二個 facade（`InventorySnapshotFacade` + 對應 router，登記為 `inventory_snapshot_facade`），加進 `app/facades/__init__.py` 觸發自我註冊，啟動真實 app 後以實際 HTTP 請求驗證：`GET /api/v1/inventory-snapshot`（新 facade 的路由）回應 200；`GET /api/v1/health` 的 `facades` 正確同時列出 `["catalog_facade", "inventory_snapshot_facade"]`；既有 `GET /api/v1/products` 等路由不受影響；全套 `pytest`（98 個既有測試）在兩個 facade 並存下依然全部通過。驗證通過後依約定**完整移除**臨時檔案（`inventory_snapshot_facade.py`、`inventory_snapshot_router.py`、`__init__.py` 裡的 import，以及殘留的 `.pyc` 快取），未留在 repo 內，也未寫成永久測試。
- [x] **5.4 新增 Plugin 自身 `version` 欄位**：`app/core/plugin_base.py` 的 `AbstractPlugin` 新增 `version: ClassVar[str] = "0.0.0"`，docstring 明確區分於 `api_version`（後者是 kernel 契約相容性、會被 loader 檢查；前者純資訊性、不參與任何判斷）。`app/api/v1/health.py` 的 `PluginHealth` 新增 `version` 欄位。新增測試驗證預設值與覆寫時兩個欄位互不影響；更新 `tests/test_api.py::test_health_check`、`docs/technical/operations.md`、`docs/technical/plugin-development.md`（`api_version` 小節補上與 `version` 的區別說明）。
- [x] **5.5 修正 `ruff.toml` 格式錯誤**：把 `[tool.ruff]`／`[tool.ruff.lint]`／`[tool.ruff.lint.isort]`（`pyproject.toml` 巢狀寫法）改成獨立 `ruff.toml` 該用的頂層鍵值＋`[lint]`／`[lint.isort]`。`ruff check` 現在能正常解析設定並執行（額外找到 79 個既有的 lint 問題，這些屬於「設定修好後才看得到」的既有程式碼風格問題，不在本項目範圍內，未處理）。未執行 `git init`——依使用者指示，基礎設施決策不主動代為執行。

## 4. 明確排除範圍（Non-goals）

- 不討論將 `user_plugin` / `product_plugin` 拆成獨立部署單元、獨立資料庫、獨立 process。
- 不討論跨服務通訊協定（HTTP client、gRPC、Message Queue、Service Mesh）。
- 不討論水平擴展、多實例部署、分散式交易一致性等微服務常見議題。
- 不重複列出 [`docs/spec/done/microkernel-architecture-improvements.md`](../done/microkernel-architecture-improvements.md)（3.1–3.14）與 [`docs/spec/done/microkernel-architecture-refinements.md`](../done/microkernel-architecture-refinements.md)（4.1–4.10）已完成的項目。
- 本文件所有建議都假設**單一 process、單一資料庫**的部署模型不變。
- 本文件刻意保守：與前兩輪不同，5.3／5.4／5.5 明確建議「暫緩」而非「照做」，因為它們的價值取決於尚未發生的未來情境，不是現況就能驗證的落差。
