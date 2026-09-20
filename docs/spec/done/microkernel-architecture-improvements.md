# Microkernel 架構強化建議

> 範圍聲明：本文件僅討論**單體部署（in-process）下的 Microkernel 架構純度**，不涉及將 plugin 拆成獨立部署的微服務、跨服務通訊（gRPC/MQ）、Service Mesh 等議題。目標是讓現有的「一個 process、多個可插拔模組」的設計，更貼近 Microkernel Architecture 的核心理念。

## 1. 現況架構總覽

```
app/core/        # kernel：plugin_base / registry / loader / hooks(EventBus) / errors / security / rate_limiter
app/infrastructure/  # 共用驅動：database / jwt / password
app/plugins/     # 可插拔模組：user_plugin, product_plugin
app/facades/     # 跨 plugin 協調：CatalogFacade
app/api/v1/      # kernel 層直接掛載的路由：health, catalog(facade)
```

現況已具備 Microkernel 的基本骨架：`AbstractPlugin` 契約、`PluginRegistry` 目錄、`PluginLoader` 動態掃描 `app/plugins/*`、`EventBus` 作為跨模組通訊管道、Result Pattern 統一錯誤語意。這些是紮實的起點，但實際實作中有多處**沒有貫徹 Microkernel 最核心的規則——kernel 與 plugin 之間、plugin 與 plugin 之間只能透過「內核提供的抽象/註冊機制」互動，不可互相直接 import 具體實作**。以下依優先度列出落差與建議。

## 2. 核心理念 vs 現況落差總覽

| Microkernel 核心原則 | 現況 | 落差程度 |
|---|---|---|
| Plugin 之間不可直接互相 import，只能透過 kernel 提供的機制通訊 | `catalog_facade.py` 直接 `import` 兩個 plugin 的 `Service` 類別與 `Product` model | 🔴 高（與 `hooks.py` 註解自述的設計原則矛盾） |
| Kernel 對 plugin 是「無知」的（不依賴具體 plugin 實作） | `app/api/v1/catalog.py`（歸在 core 路由層）直接 `import app.plugins.product_plugin.schemas` | 🔴 高 |
| Plugin 生命週期應有依賴宣告與可預期的啟動順序 | Loader 依「資料夾字母順序」register/boot，無依賴宣告、無拓樸排序 | 🟠 中 |
| Plugin 載入失敗應被隔離，不應讓整個 kernel 無法啟動、且要看得出是哪個 plugin 出問題 | `load_all()` 沒有 try/except，任何 plugin 的 `register`/`boot` 例外會直接讓 app 啟動失敗 | 🟠 中 |
| Plugin 是否載入應可由設定控制（可插可拔） | 只要資料夾存在就一定載入，沒有 enable/disable 機制 | 🟠 中 |
| Kernel 應提供顯式的能力介面給 plugin（而非讓 plugin 自行 import 全域 singleton） | `register/boot/shutdown` 只收到 `app: FastAPI`，`event_bus`、`settings`、`registry` 都是 plugin 自己 `from ... import` 全域變數 | 🟡 中低 |
| 跨模組協作應能透過事件驅動証明「零直接依賴」可行 | `EventBus.emit` 有兩處使用，但整個程式碼庫**沒有任何一個 plugin 訂閱另一個 plugin 的事件**，事件機制只有單向展示、沒有端到端驗證 | 🟡 中低 |
| Plugin 契約應可版本化，避免內核升級後舊 plugin 靜默壞掉 | `AbstractPlugin` 沒有 `api_version`／相容性檢查 | 🟢 低 |
| 架構邊界應該可被 CI 自動驗證，而不是只靠文件約定 | 沒有 import-boundary 的靜態檢查（import-linter 之類） | 🟢 低 |

## 3. 詳細改善項目

### P0 — 直接違反「Plugin 隔離」鐵律

#### 3.1 Facade 直接 import plugin 的具體實作類別

**現況問題**：`app/facades/catalog_facade.py` 直接 `from app.plugins.product_plugin.service import ProductService`、`from app.plugins.user_plugin.service import UserService`，並在建構子中 `ProductService(session)` / `UserService(session)` 直接實例化。

**為何違反理念**：`app/core/hooks.py` 的 docstring 自己寫著：「Plugins MUST use this bus instead of importing each other directly — that's the only way the kernel can guarantee isolation and safe hot-reload.」但 facade 層卻做了完全相反的事。這代表：
- 移除或抽換 `product_plugin` 資料夾，`catalog_facade.py`（甚至 `app/api/v1/catalog.py`）會直接 `ImportError`，違反「拔掉一個 plugin，其它模組仍可載入（至多在呼叫時得到降級/錯誤）」的可插拔承諾。
- Facade 對 plugin 的耦合是「編譯期強耦合」而不是「執行期弱耦合」。

**建議做法**：
- 在 `PluginRegistry` 之外（或擴充其中）新增一個 **Capability / Service Registry**，讓每個 plugin 在 `register()` 或 `boot()` 階段把自己想公開的能力註冊進去，例如：
  ```python
  # user_plugin/plugin.py
  async def boot(self, app: FastAPI) -> None:
      service_registry.provide("user_service_factory", lambda session: UserService(session))
  ```
- Facade 改為透過 `service_registry.resolve("user_service_factory")(session)` 取得服務，不再 `import` 具體類別。
- 若某個能力沒有被任何 plugin 提供（該 plugin 未載入），`resolve` 應回傳 `Result.Err`，讓 facade 可以優雅降級，而不是讓整個 import 失敗。

**影響檔案**：`app/facades/catalog_facade.py`、`app/core/registry.py`（新增 service registry）、`app/plugins/*/plugin.py`（新增 `boot()` 內的註冊呼叫）。

#### 3.2 Kernel 層路由直接 import plugin 的 DTO

**現況問題**：`app/api/v1/catalog.py` 被放在「core 路由層」，卻直接 `import app.plugins.product_plugin.schemas`（`ProductCreate` / `ProductResponse` 等）。

**為何違反理念**：`api/v1/` 目錄的定位是 kernel 掛載的核心路由（跟 `health.py` 同一層級），理論上 kernel 不該對某個具體 plugin 的資料結構有直接依賴——這條路由的本質其實是「CatalogFacade 的 HTTP 外殼」，語意上更接近一個 plugin（或至少是 facade 自帶的路由），不該混在 core 層。

**建議做法**：
- 將 `catalog.py` 的路由改為由 facade 自己註冊（例如新增 `app/facades/catalog_facade.py` 旁的 `router.py`，並在啟動流程中由一個顯式的「facade 註冊」步驟掛載，而不是塞進 `api/v1/`）。
- 或者：facade 對外只回傳 kernel 定義的通用 DTO（例如 `dict` / 一個不屬於任何 plugin 的通用 Pydantic model），不直接 reuse plugin 的 schema，維持「facade 的輸出契約獨立於任何單一 plugin」的方向性依賴。

**影響檔案**：`app/api/v1/catalog.py`、`app/main.py`（路由掛載位置）。

#### 3.3 Plugin 生命週期無依賴宣告與拓樸排序

**現況問題**：`PluginLoader._discover()` 用 `sorted(_PLUGINS_DIR.iterdir())`，`load_all()` 依此固定順序 register 全部、再依此固定順序 boot 全部。目前恰好 `product_plugin` 不依賴 `user_plugin` 的 boot 結果，所以沒爆炸，但這只是巧合。

**為何違反理念**：Microkernel 的 plugin 生命週期管理應該保證「A 依賴 B」時 B 一定先 boot 完成。目前完全靠檔名字母序，一旦新增一個依賴其他 plugin 已完成初始化的 plugin（例如「歡迎信 plugin」需要 `user_plugin` 先把某個 provider 註冊好），排序方式無法保證正確性,且沒有任何機制可以提前發現問題。

**建議做法**：
- 在 `AbstractPlugin` 新增 `dependencies: ClassVar[list[str]] = []`。
- `PluginLoader._discover()` 之後，用簡單的拓樸排序（DFS/Kahn's algorithm）決定 register/boot 順序；偵測到循環依賴或缺少依賴時，啟動失敗並給出明確錯誤訊息（目前完全沒有這類防呆）。

**影響檔案**：`app/core/plugin_base.py`、`app/core/loader.py`。

#### 3.4 Plugin 載入失敗沒有隔離與清楚診斷

**現況問題**：`load_all()` 中 `register()`／`boot()` 呼叫沒有 try/except；`unload_all()` 反而有完整的 try/except/finally。等於「拆解」的容錯做得好，「組裝」完全沒做。

**為何影響 Microkernel 品質**：Plugin 系統的價值之一，是單個模組壞掉不該拖垮整個 kernel（至少要能清楚定位問題）。目前若 `product_plugin.register()` 丟例外，整個 app 啟動失敗，錯誤訊息只是一般的 traceback，看不出「是哪個 plugin、哪個生命週期階段」出錯（雖然有 log，但沒有結構化的失敗狀態）。

**建議做法**：
- 為 `PluginRegistry` 加上狀態追蹤：`PENDING / REGISTERED / BOOTED / FAILED`。
- `load_all()` 對每個 plugin 的 register/boot 加 try/except，失敗時標記為 `FAILED`、記錄清楚的 log、並依設定決定「fail-fast（整個 app 啟動失敗）」或「best-effort（跳過該 plugin，其餘正常啟動）」。
- `health` endpoint 曝露每個 plugin 的狀態，而不只是名字列表（見 3.8）。

**影響檔案**：`app/core/loader.py`、`app/core/registry.py`、`app/api/v1/health.py`。

---

### P1 — Microkernel 能力尚未完整落地

#### 3.5 Plugin 無法透過設定啟用/停用

**現況問題**：`_discover()` 只要資料夾存在且有 `plugin` 屬性就會被載入，沒有任何「這個環境要不要載入這個 plugin」的開關。

**建議做法**：
- 在 `Settings` 增加 `enabled_plugins: list[str] | None = None`（`None` 代表全部載入，指定清單則只載入清單內的 plugin）。
- `_discover()` 依此清單過濾。這是 Microkernel「執行期決定要哪些擴充」的核心體感——同一份程式碼在不同環境（dev/staging/prod）可以裝載不同組合的 plugin，而不需要改程式碼或刪檔案。

**影響檔案**：`app/config.py`、`app/core/loader.py`。

#### 3.6 EventBus 缺少「真正跨 plugin」的訂閱示範

**現況問題**：`ProductService`／`UserService` 都有 `event_bus.emit(...)`（`product.created`、`user.created`、`product.stock_adjusted`），但檢視全部程式碼，**沒有任何地方呼叫 `event_bus.subscribe(...)`**（除了測試檔案）。也就是說目前的 EventBus 是一個「只發射、沒人接收」的示範，沒有真正驗證過「plugin A 不 import plugin B、只靠事件完成協作」這件事在這個 loader 生命週期下能不能正確運作（例如訂閱應該在哪個階段做才安全？`register()` 還是 `boot()`？）。

**建議做法**：
- 至少補一個端到端範例：例如 `user_plugin` 在 `boot()` 階段訂閱 `product.created`，寫一筆稽核 log；或反過來 `product_plugin` 訂閱 `user.created` 做初始化動作。
- 在 README/plugin 契約文件中明訂：**訂閱動作應該放在 `boot()`，因為此時所有 plugin 都已完成 `register()`**（`register()` 階段還不能保證其他 plugin 已存在），並補上對應測試，證明「拔掉發送方 plugin 之後，接收方 plugin 仍可正常載入（只是收不到事件）」。

**影響檔案**：`app/plugins/*/plugin.py`、`tests/`。

#### 3.7 Plugin 契約沒有版本化

**現況問題**：`AbstractPlugin` 沒有任何欄位標示自己相容於哪個 kernel 介面版本。

**建議做法**：Kernel 定義 `KERNEL_API_VERSION`（例如 `"1.0"`），`AbstractPlugin` 增加 `api_version: str` 或相容區間宣告，`PluginLoader._discover()` 在載入前檢查相容性，不相容時明確拒絕並記錄，而不是等到執行期某個方法簽名不對才炸掉。這對「這是個可長期擴充的內核」而不是「寫死兩個 plugin 的範例」很重要。

**影響檔案**：`app/core/plugin_base.py`、`app/core/loader.py`。

#### 3.8 Kernel 未向 plugin 提供顯式的能力介面（Kernel Context）

**現況問題**：`register/boot/shutdown` 的簽章只有 `app: FastAPI`。Plugin 若要用 `event_bus`、`settings`，都是各自 `from app.core.hooks import event_bus` 這種「隱性全域依賴」，不是透過契約傳入的。

**建議做法**：定義一個 `KernelContext`（dataclass 或簡單 class），內含 `event_bus`、`settings`、`service_registry`、`db_factory` 等 kernel 提供的能力，`register(self, app, ctx)` 這樣顯式傳入。好處：
- Plugin 的依賴一次看清楚，不用去翻 import。
- 未來要 mock/替換某個 kernel 能力（例如測試時用假的 event_bus）時，不需要 monkeypatch 全域 singleton。

**影響檔案**：`app/core/plugin_base.py`、`app/core/loader.py`、所有 `app/plugins/*/plugin.py`。

#### 3.9 Health/Introspection 端點資訊過於單薄

**現況問題**：`GET /api/v1/health` 只回傳 `plugins: list[str]`（名字陣列），無法看出哪個 plugin 是否成功 boot、有無失敗、依賴關係、版本。

**建議做法**：搭配 3.4 的狀態追蹤與 3.7 的版本欄位，把 health/introspection 端點擴充成可以看到每個 plugin 的 `{name, state, version, dependencies}`，作為單體應用內建的「模組管理面板」雛形。

**影響檔案**：`app/api/v1/health.py`、`app/core/registry.py`。

#### 3.10 Facade 層沒有正式抽象與註冊機制

**現況問題**：`CatalogFacade` 是唯一一個 facade，沒有基底類別、沒有註冊表，未來新增第二個 facade 時容易各自為政（不同的建構子慣例、沒有統一的 introspection 入口）。

**建議做法**：定義 `AbstractFacade`（可以很輕量，只要求 `name` 與建構子接受 `session`），並提供 `facade_registry`，讓 facade 的存在也能像 plugin 一樣被列舉與管理，同時保持「facade 依賴 plugin 提供的能力（透過 3.1 的 service registry），plugin 不反向依賴 facade」的單向依賴規則。

**影響檔案**：新增 `app/facades/base.py`、`app/facades/registry.py`。

---

### P2 — 長期精進 / 架構治理

#### 3.11 缺少「架構邊界」的自動化檢查

**現況問題**：以上大部分問題（facade 直接 import plugin 類別、core 路由 import plugin schema）目前完全靠人工 code review 把關，沒有任何自動化機制阻止「不小心又寫了一個跨 plugin 直接 import」。

**建議做法**：引入 `import-linter`（或自訂一個簡單的 AST/`ast.walk` 腳本），在 CI 中定義規則，例如：
- `app.plugins.*` 之間互相 import 視為違規（僅可透過 `app.core.hooks` / service registry）。
- `app.api.v1.*`（core 路由）不可 import `app.plugins.*.schemas` / `app.plugins.*.service` / `app.plugins.*.models`。
- `app.core.*`（kernel）不可 import 任何 `app.plugins.*`。

這能把本文件列的邊界規則，從「文件裡的約定」變成「PR 過不了就是壞了規則」，是讓 Microkernel 邊界長期維持住的關鍵。

**影響檔案**：新增 `pyproject.toml`/`importlinter.ini` 設定與對應 CI 步驟。

#### 3.12 缺少熱插拔（Hot Reload）路徑

**現況問題**：`PluginRegistry.unregister()` 的註解寫「Used during hot-reload」，但目前整個系統只在 `lifespan` 的 startup/shutdown 觸發一次 `load_all`/`unload_all`，沒有任何執行期觸發單一 plugin 卸載/重載的入口（無 admin API、無 CLI）。

**建議做法**：新增一個受保護（admin-only）的管理端點或 CLI 指令，實作「卸載單一 plugin → 清除其路由/事件訂閱 → 重新 import → register/boot」的流程，並在文件中明確標註目前的限制（例如：SQLAlchemy 的 `Base.metadata`／engine 是全域單例，Schema 變更不在熱重載範圍內，僅適合 route/service 邏輯層級的重載)。這是把「動態載入」從「僅在啟動時發生一次」提升到「真正的執行期可插拔」的關鍵一步。

**影響檔案**：`app/core/loader.py`、新增管理端點。

#### 3.13 跨 Plugin 資料層邊界沒有寫成規範

**現況問題**：目前 `Product`／`User` 模型之間沒有跨 plugin 的 Foreign Key，這是好現象，但這只是「目前沒人這麼做」，並非「架構強制不能這麼做」。

**建議做法**：在架構文件中明訂：「Plugin 之間不可直接 query 對方的 table，也不可建立跨 plugin 的 Foreign Key；跨模組資料存取必須經過對方的 Service（透過 3.1 的 service registry）或事件通知」，並可透過 3.11 的靜態檢查延伸驗證 model 檔案間沒有互相 import。

**影響檔案**：架構文件（本文件的延伸）、`app/plugins/*/models.py` 的 review 準則。

#### 3.14 缺少新增 Plugin 的 Scaffold 工具

**現況問題**：README 用手寫範例示範怎麼建立一個 plugin，但沒有腳本協助生成符合規範的骨架，容易漏掉步驟（例如忘記在 `register()` 內 import model、忘記加測試骨架、忘記宣告 `dependencies`/`api_version`）。

**建議做法**：新增一個簡單的 scaffold 腳本（例如 `scripts/new_plugin.py <name>`），依本文件定案後的最新 `AbstractPlugin` 契約產生骨架檔案（`plugin.py` / `router.py` / `service.py` / `models.py` / `schemas.py` / 測試檔），降低「新 plugin 沒有遵守 Microkernel 邊界規則」的機率。

**影響檔案**：新增 `scripts/new_plugin.py`。

## 4. 建議導入順序（分階段，不涉及微服務化）

| 階段 | 項目 | 理由 |
|---|---|---|
| Phase 1（先修正矛盾） | 3.1 Service Registry、3.2 Facade 路由歸位、3.4 載入失敗隔離 | 這三項是目前**自我矛盾**的地方（文件說一套、程式碼做另一套），優先修正才能讓後續規則有意義。 |
| Phase 2（補齊生命週期治理) | 3.3 依賴宣告與拓樸排序、3.5 可設定啟用/停用、3.9 Health 端點擴充 | 讓「plugin 可插拔」從「理論上可以」變成「有機制保證」。 |
| Phase 3（驗證松耦合真的成立） | 3.6 EventBus 端到端示範、3.8 Kernel Context、3.10 Facade 抽象 | 補齊跨模組協作的正確示範與顯式契約，降低隱性耦合。 |
| Phase 4（治理與長期維護） | 3.7 版本化、3.11 CI 邊界檢查、3.12 熱插拔、3.13 資料層規範、3.14 Scaffold 工具 | 讓架構邊界可以長期自動維持，而不是每次 review 靠記憶力。 |

## 5. 明確排除範圍（Non-goals）

- 不討論將 `user_plugin` / `product_plugin` 拆成獨立部署單元、獨立資料庫、獨立 process。
- 不討論跨服務通訊協定（HTTP client、gRPC、Message Queue、Service Mesh）。
- 不討論水平擴展、多實例部署、分散式交易一致性等微服務常見議題。
- 本文件的所有建議都假設**單一 process、單一資料庫**的部署模型不變，優化重點是「模組邊界的純度與治理」，而不是「拆分部署」。

## 6. 實作 Checklist（Phase 1–4）

> 狀態：Phase 1、2、3、4 全數實作完成（3.1–3.14 全部條目），每個 Phase 結束後都執行過全套測試並全數通過。本節依 Phase 記錄實際改動、測試結果，以及過程中額外發現並經核准修正的既有缺陷。

### 6.1 Phase 1 — 修正矛盾

- [x] **3.1 Service Registry**：`app/core/registry.py` 新增 `ServiceRegistry`／`service_registry` 單例（`provide()`/`resolve()`/`clear()`，`resolve()` 回傳 `Result`）。`product_plugin`、`user_plugin` 在各自 `boot()` 呼叫 `service_registry.provide("product_service_factory", ...)` / `("user_service_factory", ...)`。`app/facades/catalog_facade.py` 改為透過 `service_registry.resolve(...)` 取得服務，不再直接 `import ProductService`/`UserService`。
- [x] **3.2 Facade 路由歸位**：`app/api/v1/catalog.py` 移除，內容搬到新檔 `app/facades/router.py`；`app/main.py` 改為兩個獨立步驟掛載路由（core `health` 一段、facade `catalog` 另一段），並在程式碼註解中說明理由。
- [x] **3.4 Plugin 載入失敗隔離**：`app/core/registry.py` 新增 `PluginState`（`PENDING/REGISTERED/BOOTED/FAILED/SHUTDOWN`）與 `PluginRegistry.set_state()/state_of()/error_of()/states()`。`app/core/loader.py` 的 `load_all()` 對每個 plugin 的 `register()`/`boot()` 加上 try/except，失敗時標記 `FAILED` 並依 `settings.plugin_load_mode`（新增於 `app/config.py`，`fail_fast`/`best_effort`）決定是否中止整個啟動。`unload_all()` 補上 `service_registry.clear()`，避免殘留能力污染下一次啟動。

### 6.2 Phase 2 — 補齊生命週期治理

- [x] **3.3 依賴宣告與拓樸排序**：`app/core/plugin_base.py` 的 `AbstractPlugin` 新增 `dependencies: ClassVar[list[str]] = []`。`app/core/loader.py` 新增 `_resolve_load_order()`（Kahn's algorithm），依此決定 `register()`/`boot()` 順序；缺少依賴或循環依賴一律拋出 `PluginLoadError`、直接中止啟動（不受 `plugin_load_mode` 影響，因為這是設定錯誤而非單一 plugin 執行期失敗）。額外補上：當某 plugin 在 `best_effort` 模式下失敗時，任何宣告依賴它的 plugin 會被連鎖標記為 `FAILED`（`skipped: dependency failed: [...]`）並跳過，不會誤以為依賴已就緒。
- [x] **3.5 可設定啟用/停用**：`app/config.py` 新增 `enabled_plugins: list[str] | None`（`None` = 全部載入）。`app/core/loader.py` 的 `_discover()` 依此清單過濾要 `import` 的 plugin 目錄。
- [x] **3.9 Health 端點狀態擴充**：`app/api/v1/health.py` 回傳結構從 `plugins: list[str]` 改為 `plugins: list[{name, state, dependencies, error}]`。**註記**：原規劃提到「搭配 3.7 的版本欄位」，但 3.7（Plugin 契約版本化）屬於 Phase 3，本輪未實作，因此健康檢查目前不含 `version` 欄位，待 3.7 完成後再補上。

### 6.3 額外發現並已修正的既有缺陷（經使用者核准，非本文件 Phase 1/2 條目，但屬於讓「全面測試」有意義執行的必要前提）

以下問題與 Microkernel 架構理念無關，是修復過程中發現的既有程式/測試缺陷；每一項都先向使用者說明並取得同意後才動手：

- [x] `requirements.txt` 缺少 `email-validator`（`pydantic.EmailStr` 需要）與 `python-multipart`（`OAuth2PasswordRequestForm` 需要）——原本連 import/表單登入都會炸掉。
- [x] `requirements.txt` 的 `passlib[bcrypt]` 與新版 `bcrypt`（≥4.1，移除了 `__about__`）不相容，導致密碼雜湊全部失敗；已釘住 `bcrypt<4.1`。
- [x] `tests/conftest.py` 的 `client` fixture 從未觸發 FastAPI `lifespan`，導致 `PluginLoader` 從未真正執行，`user_plugin`/`product_plugin` 的路由在 HTTP 測試中全部 404（`test_api.py` 實質上沒有測到這些路由）。已改為 `async with app.router.lifespan_context(app):` 包住整個 client 生命週期，並在最前面設定 `DATABASE_URL=sqlite+aiosqlite:///:memory:` 避免產生 `dev.db` 檔案。
- [x] **修正後才暴露的更嚴重 bug**：`product_plugin/plugin.py`、`user_plugin/plugin.py` 的 `register(self, app: FastAPI)` 內用 `import app.plugins.xxx.models`（未加 `as` 別名）觸發 model 載入，這行 import 會把參數 `app` 重新綁定成 `app` 套件本身，導致下一行 `app.include_router(...)` 拋出 `AttributeError`。實測證實**真實用 `uvicorn app.main:app` 啟動也會直接 crash**，不只是測試環境問題。已改為 `import ... as _models`。
- [x] 附帶修正：`tests/test_user_plugin.py::test_authenticate_wrong_password` 使用 7 字元密碼 `"correct"`，但 `UserCreate.password` 規則是 `min_length=8`，屬既有測試資料 typo；改為 `"correctpw"`。
- [x] 附帶修正：`app/core/rate_limiter.py` 的 `limiter` 是行程級單例，測試 session 之間的請求次數會累加，導致新增的 facade 測試把 `/auth/register` 的 `10/minute` 限制打爆而回 429。已在 `client` fixture 開頭呼叫 `limiter.reset()`。
- [ ] （僅觀察、未修正）`ruff.toml` 本身有既有格式錯誤（用了 `[tool.ruff]` 區塊，但獨立 `ruff.toml` 應直接用 `[lint]`），導致 `ruff check` 無法執行。與本次任務無關，未列入本輪修正範圍。

### 6.4 Phase 3 — 驗證松耦合真的成立

- [x] **3.8 Kernel Context**：`app/core/plugin_base.py` 新增 `KernelContext`（frozen dataclass：`event_bus`/`settings`/`service_registry`/`db_factory`）；`AbstractPlugin.register/boot/shutdown` 的抽象簽章改為 `(self, app, ctx: KernelContext)`。`app/core/loader.py` 建構 `KernelContext` 並在每個生命週期呼叫時傳入。`product_plugin`、`user_plugin` 的 `plugin.py` 改為透過 `ctx.service_registry`/`ctx.event_bus` 取得能力，移除對 `app.core.registry`/`app.core.hooks` 單例的直接 import。同步更新 `tests/test_core.py` 內全部 fake plugin（約 10 個類別）的方法簽章。
- [x] **3.6 EventBus 端到端示範**：`user_plugin` 在 `boot()` 透過 `ctx.event_bus.subscribe("product.created", ...)` 訂閱 `product_plugin` 發出的事件並寫稽核 log，且**完全沒有 import** `app.plugins.product_plugin` 任何東西。新增 `tests/test_events.py`：(1) HTTP 端到端測試，透過 facade 建立商品後用 `caplog` 驗證稽核 log 真的觸發；(2) 用 `enabled_plugins=["user_plugin"]` 排除 `product_plugin`，證明 `user_plugin` 仍可正常 `BOOTED`（只是收不到事件）。
- [x] **3.10 Facade 抽象與註冊機制**：新增 `app/facades/base.py`（`AbstractFacade`）與 `app/facades/registry.py`（`FacadeRegistry`/`facade_registry`）。`CatalogFacade` 改為繼承 `AbstractFacade`（`name = "catalog_facade"`）並在檔案底部呼叫 `facade_registry.register(CatalogFacade)` 自我註冊。新增對應測試（自我註冊、重複註冊、無名稱皆會報錯）。

Phase 3 完成後執行 `pytest -q` → **66 passed**，重跑確認穩定、無殘留檔案。

### 6.5 Phase 4 — 治理與長期維護

- [x] **3.7 Plugin 契約版本化**：`app/core/plugin_base.py` 新增 `KERNEL_API_VERSION = "1.0"`、`AbstractPlugin.api_version`（預設等於當前版本）、`is_api_version_compatible()`（僅比對主版號）。`app/core/loader.py` 的 `_discover()` 在載入前檢查相容性，不相容則跳過並記錄警告。同時完成 Phase 2 checklist 中記錄的待辦：`app/api/v1/health.py` 補上 `api_version` 欄位。
- [x] **3.11 架構邊界自動化檢查**：新增 `scripts/check_architecture_boundaries.py`（純 AST 掃描，未新增依賴），實作文件所列三條規則；`tests/test_architecture_boundaries.py` 將其接入 `pytest` 作為自動化 gate。**已用刻意注入的違規手動驗證過三條規則都能正確抓到**（跑完即還原，未留在 repo 內）。因為本專案目前不是 git repository（`git status` 確認過），沒有可掛載 GitHub Actions 的地方，所以未新增 `.github/workflows`；改以 `pytest`（本專案唯一現存的自動化關卡）作為強制執行點，待專案加入版本控制/CI 後可再补上 workflow 檔案。
- [x] **3.12 熱插拔（Hot Reload）**：`ServiceRegistry`/`EventBus` 新增 `owner` 標記與 `revoke_all_from()`/`unsubscribe_all_from()`；新增 `ScopedServiceRegistry`/`ScopedEventBus`，`PluginLoader` 改為對每個 plugin 用 `_build_ctx_for(name)` 建構「綁定該 plugin 名稱」的專屬 `KernelContext`。新增 `PluginLoader.reload_one(name)`：卸載 → 從 `app.router.routes` 移除該 plugin 的路由 → 撤銷其 service/event 註冊 → 重新 `importlib.reload()` 其 `service.py`/`router.py`/`plugin.py`（**刻意不重載 `models.py`/`schemas.py`**，因為 SQLAlchemy `Base.metadata` 是行程級單例，無法重新定義同一張表的 mapped class）→ 用新 instance 重新 `register()`+`boot()`。新增受保護端點 `POST /api/v1/admin/plugins/{name}/reload`（沿用既有 `require_admin`），`main.py` 將 loader 存進 `app.state.plugin_loader` 供端點使用。新增 `tests/test_hot_reload.py`：底層 `revoke_all_from`/`unsubscribe_all_from`/`Scoped*` 單元測試，以及對真實執行中 app 呼叫 reload 端點、驗證路由與 service 能力都恢復正常的端到端測試。
- [x] **3.13 跨 Plugin 資料層規範**：`app/infrastructure/database.py` 的 `Base` docstring 明訂規則（不可建立跨 plugin FK、不可直接 query 對方 model）；README 新增「Data layer boundaries」小節。Import 這一半的規則已被 3.11 的檢查器自動涵蓋（Rule 1 不分 `models`/`service`/`schemas`，任何跨 plugin import 都會被抓到）；FK/query 這一半無法靜態檢查，維持 code review 準則。
- [x] **3.14 Plugin Scaffold 工具**：新增 `scripts/new_plugin.py <name>`，依目前最新的 `AbstractPlugin` 契約（`ctx`、`dependencies`、`api_version`）產生 `plugin.py`/`router.py`/`service.py`/`models.py`/`schemas.py`/`__init__.py` 與一個測試骨架。**已在暫存複本專案實際跑過一次完整流程**驗證（非僅紙上設計）：產生 `billing_plugin` → `check_architecture_boundaries.py` 通過 → 產生的測試通過 → 整個專案 `pytest` 全套通過。過程中發現 `tests/conftest.py` 的 `engine` fixture 原本硬編碼只 import `user_plugin`/`product_plugin` 的 models，導致新 scaffold 出的 plugin 測試找不到資料表（500）；經確認後已改為動態掃描 `app/plugins/*` 自動 import 所有 plugin 的 models（見 6.6）。真實 repo 內另新增 `tests/test_scaffold.py`，對 `_render()` 產出的程式碼做語法/契約靜態驗證（不落地檔案，避免 pytest 執行時在 repo 建立實體目錄）。

Phase 4 完成後執行 `pytest -q` → **82 passed**，重跑確認穩定、無殘留檔案。

### 6.6 Phase 4 過程中額外發現並已修正的既有缺陷（經核准）

- [x] `tests/conftest.py` 的 `engine` fixture 原本硬編碼 `import app.plugins.user_plugin.models` / `import app.plugins.product_plugin.models` 兩行，與「plugin 應被動態發現」的整體理念不一致，也導致新 scaffold 出的 plugin 測試會 500（`no such table`）。已改為掃描 `app/plugins/*` 目錄動態 import 所有 models（邏輯與 `PluginLoader._discover()` 一致）。

### 6.7 整體測試結果彙總

| 階段 | 指令 | 結果 |
|---|---|---|
| Phase 1 | `pytest -q` | 54 passed |
| Phase 2 | `pytest -q` | 60 passed |
| Phase 3 | `pytest -q` | 66 passed |
| Phase 4 | `pytest -q` | 82 passed |

每個階段結束都重跑至少兩次確認結果穩定（非偶然通過），且執行後專案目錄未留下 `dev.db` 等副作用檔案。至此，本文件第 3 節列出的 **3.1–3.14 全部 14 個項目均已實作完成**，第 4 節的 Phase 1–4 導入順序全數走完。
