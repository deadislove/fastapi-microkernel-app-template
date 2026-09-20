# Microkernel 架構強化建議（第二輪：貫徹一致性與治理精進）

> 範圍聲明：本文件僅討論**單體部署（in-process）下的 Microkernel 架構純度**，不涉及將 plugin 拆成獨立部署的微服務、跨服務通訊（gRPC/MQ）、Service Mesh 等議題。延續 [`docs/spec/done/microkernel-architecture-improvements.md`](./microkernel-architecture-improvements.md) 的方向，目標是讓現有「一個 process、多個可插拔模組」的設計更貼近 Microkernel Architecture 的核心理念。

## 0. 背景

[`docs/spec/done/microkernel-architecture-improvements.md`](./microkernel-architecture-improvements.md) 列出的 14 個項目（3.1–3.14）已全部實作完成並經過 Phase 1–4 的全面測試驗證（最終 82 個測試通過）。目前系統已具備：

- `KernelContext`（`event_bus`／`settings`／`service_registry`／`db_factory`）顯式傳入每個 plugin 的 `register()`/`boot()`/`shutdown()`。
- `ServiceRegistry`／`EventBus` 支援 `owner` 標記與 `ScopedServiceRegistry`／`ScopedEventBus`，讓 hot-reload 能精準撤銷單一 plugin 發布的能力與訂閱。
- `PluginRegistry` 的狀態機（`PENDING/REGISTERED/BOOTED/FAILED/SHUTDOWN`）與依賴宣告（`dependencies`）+ 拓樸排序 + `fail_fast`/`best_effort` 失敗隔離。
- `AbstractFacade`／`FacadeRegistry`，`CatalogFacade` 已改用並自我註冊。
- `AbstractPlugin.api_version` 相容性檢查、`GET /api/v1/health` 完整狀態曝露。
- `scripts/check_architecture_boundaries.py`（3 條規則）已接入 `pytest` 作為自動化 gate。
- `POST /api/v1/admin/plugins/{name}/reload` 熱插拔端點。
- `scripts/new_plugin.py` scaffold 工具。

本輪重新檢視「這些機制實作完成後，彼此之間是否真正一致、有沒有新機制沒有貫徹到底」，找到的問題多半是**上一輪引入的新抽象尚未貫徹到全系統**所產生的不一致，而非全新的架構違規。以下依驗證過的程式碼位置逐項說明。

## 1. 落差總覽

| 項目 | 現況 | 落差程度 |
|---|---|---|
| `KernelContext` 沒有貫徹到 Service 層 | `product_plugin/service.py`、`user_plugin/service.py` 都直接 `from app.core.hooks import event_bus` 來 emit 事件，因為 `ctx` 只存在於 plugin 生命週期鉤子，per-request 建構的 Service 實例拿不到它 | 🟠 中 |
| Facade 層從未真正拿到 `KernelContext` | `app/facades/catalog_facade.py` 仍直接 `from app.core.registry import service_registry`，沒有走與 plugin 一致的顯式注入路徑 | 🟠 中 |
| `FacadeRegistry` 只是登記冊，不驅動路由掛載 | 新增第二個 facade 時，`app/main.py` 仍須手動 `import` 並 `include_router`；plugin 則是 loader 自動發現、完全不用碰 `main.py` | 🟠 中 |
| 架構邊界檢查沒有「繞過 ctx」規則 | `scripts/check_architecture_boundaries.py` 只有 3 條規則，沒有檢查 plugin 是否繞過 `ctx.service_registry`/`ctx.event_bus`、直接 import `app.core.registry.service_registry`/`app.core.hooks.event_bus` 全域單例 | 🟡 中低 |
| 熱重載沒有依賴圖感知 | `PluginLoader.reload_one()` 只處理單一 plugin，重載一個被其他 plugin 宣告為 `dependencies` 的 plugin 時，不會通知、跳過或重載其依賴者 | 🟡 中低 |
| `enabled_plugins` 與 `dependencies` 交互時處理生硬 | 透過設定停用一個被其他 plugin 依賴的 plugin時，`_resolve_load_order()` 會直接拋出 `PluginLoadError` 讓整個 kernel 啟動失敗，而非把依賴它的 plugin 一併排除並給出更友善訊息 | 🟡 中低 |
| `PluginLoader` 對外部注入的支援不對稱 | 建構子已支援注入 `event_bus`/`settings`/`service_registry`/`db_factory`，但 `plugin_registry` 仍是寫死的全域 import，測試/呼叫端無法比照辦理注入假的 registry | 🟢 低 |
| Health/introspection 端點沒有列出 facade | `GET /api/v1/health` 只曝露 plugin 狀態，沒有一併列出 `facade_registry.names()` | 🟢 低 |
| 沒有 plugin 專屬設定命名空間 | 所有 plugin 共用同一個全域 `Settings`；plugin 若要有自己的設定選項，只能直接塞進 `app/config.py`，讓 kernel 的設定檔隨 plugin 數量膨脹 | 🟢 低 |
| 熱重載未處理 plugin 可能持有的背景資源 | `reload_one()` 撤銷路由/服務/事件訂閱，但若某 plugin 在 `boot()` 期間啟動了 asyncio background task／排程器，目前沒有任何追蹤與取消機制 | 🟢 低（現有兩個 plugin 未使用，屬於未來風險） |

## 2. 詳細改善項目

### 4.1 `KernelContext` 沒有貫徹到 Service 層

**現況問題**：`app/plugins/product_plugin/service.py` 與 `app/plugins/user_plugin/service.py` 都在模組頂層 `from app.core.hooks import event_bus`，並在 `create()`/`register()`/`adjust_stock()` 等方法內直接呼叫 `event_bus.emit(...)`。

**為何是落差**：3.8 引入 `KernelContext` 的初衷是「plugin 對 kernel 的依賴應該顯式、可替換，而不是暗藏在 import 裡」。但 `ctx` 目前只存在於 `AbstractPlugin.register/boot/shutdown` 的參數列表——真正執行商業邏輯、真正呼叫 `emit()` 的 `Service` 物件是透過 `service_registry` 的 factory 在**每個 HTTP 請求時**建構的（`lambda session: ProductService(session)`），這條路徑完全沒有機會拿到 `ctx`。於是「不要直接 import kernel 單例」這條原則，在 Service 層直接被繞過。

需要說明的是：這**目前不是安全性問題**——`emit()` 是無狀態的廣播，不需要 `owner` 標記，不影響 hot-reload 的撤銷邏輯（`unsubscribe_all_from`/`revoke_all_from` 只影響 `subscribe()`/`provide()`）。但這是一個明顯的**一致性缺口**：`KernelContext` 沒有覆蓋到系統中「真正最常執行、真正做跨模組協作」的那一層。

**建議做法**：
- 讓 `ServiceRegistry.provide()` 註冊的 factory 簽章統一改為 `(session, ctx) -> Service`，`ScopedServiceRegistry.resolve()` 回傳的 callable 也接受 `ctx`；`ProductService.__init__(self, session, ctx)` 把 `ctx.event_bus` 存成 `self._event_bus`，emit 時改用 `self._event_bus.emit(...)`。
- 或者：接受目前的設計（emit 不需要 scoping），但把這個取捨明確寫進 `app/core/hooks.py` 的 docstring 與 [plugin-development.md](../../technical/plugin-development.md)，避免未來的開發者誤以為「所有情況都該透過 ctx」而製造不必要的重構。

**影響檔案**：`app/core/registry.py`（`ServiceRegistry`/`ScopedServiceRegistry` 的 factory 簽章）、兩個 plugin 的 `service.py`/`plugin.py`、`docs/technical/plugin-development.md`。

### 4.2 Facade 層從未真正拿到 `KernelContext`

**現況問題**：`app/facades/catalog_facade.py` 建構時只收 `session`（繼承自 `AbstractFacade.__init__(self, session)`），要用到 `service_registry` 時直接 `from app.core.registry import service_registry` 取模組單例。

**為何是落差**：與 4.1 同源——`resolve()` 不需要 `owner` 標記，所以這在功能上是安全的，但破壞了「所有跨模組協作都必須顯式透過 ctx」的一致敘事。更實際的影響是：facade 目前完全沒有生命週期、沒有辦法比照 plugin 做熱插拔或注入假的 `service_registry` 來測試（現有測試也確實是直接呼叫 `service_registry.resolve` 驗證，見 `tests/test_core.py` 的 `test_catalog_facade_is_self_registered`，而不是透過某種 facade 專屬的 DI）。

**建議做法**：
- 為 `AbstractFacade` 增加一個可選的 `ctx: KernelContext | None` 建構參數（或改為 `classmethod create(cls, session, ctx)`），呼叫端（`app/facades/router.py`）從 `request.app.state`（比照 `admin.py` 取得 `plugin_loader` 的方式）取得目前的 `KernelContext` 並傳入。
- 這也為「facade 熱插拔」鋪路：若未來要讓 `reload_one()` 能重載 facade 本身（例如 facade 邏輯改了但不想重啟），需要 facade 先有一致的 ctx 注入路徑。

**影響檔案**：`app/facades/base.py`、`app/facades/catalog_facade.py`、`app/facades/router.py`、`app/main.py`（需要把 `KernelContext` 存到 `app.state`，目前只存了 `plugin_loader`）。

### 4.3 `FacadeRegistry` 只是登記冊，不驅動路由掛載

**現況問題**：`facade_registry.register(CatalogFacade)` 只是把 class 存進一個 `dict`；真正讓 `/api/v1/catalog/*` 生效的是 `app/main.py` 手動 `from app.facades.router import router as catalog_facade_router` + `app.include_router(...)`。

**為何是落差**：Plugin 的賣點之一是「新增一個 plugin 完全不用碰 `main.py`／`loader.py`」——目錄一放，loader 自動發現。Facade 卻沒有對等的體驗：新增第二個 facade（例如未來的 `OrderFacade`）仍必須手動編輯 `main.py`，這與「facade 也有自己的 registry」給人的期待不一致。

**建議做法**：
- 讓每個 facade 模組除了自我註冊 class，也在同一個地方（或約定 `app/facades/<name>/router.py`）暴露一個 `router` 屬性；`main.py` 改成遍歷 `facade_registry` 動態 `include_router`，而不是逐一手動 import。
- 若暫時不想大改，至少在 `FacadeRegistry` 或 docstring 中明確記錄「目前路由掛載仍是手動步驟，`facade_registry` 只保證『可列舉』，不保證『自動掛載』」，避免文件與實作出現認知落差。

**影響檔案**：`app/facades/registry.py`、`app/main.py`。

### 4.4 架構邊界檢查沒有「繞過 ctx」規則

**現況問題**：`scripts/check_architecture_boundaries.py` 目前的三條規則只檢查「plugin 互相 import」「kernel 路由 import plugin 內部」「kernel import plugin」，沒有檢查「plugin 的程式碼是否繞過 `ctx.service_registry`/`ctx.event_bus`，直接 `import app.core.registry.service_registry` 或 `app.core.hooks.event_bus`」——而 4.1 已經證實這個繞過**目前確實發生**（兩個 plugin 的 `service.py` 都這樣做）。

**為何是落差**：如果決定保留 4.1 的現狀（emit 不需要 scoping，允許 Service 層直接 import），這條規則應該明確排除 `service.py` 這種案例，只針對 `plugin.py`（生命週期鉤子）做檢查；如果決定照 4.1 的建議修正，這條規則就該變成第 4 條硬性規則，防止未來又有人在 `plugin.py` 裡繞過 `ctx.service_registry.provide(...)` 直接呼叫 `service_registry.provide(...)`。無論選哪個方向，現狀是「規則存在空白，程式碼與文件的界線沒有被自動化強制」。

**建議做法**：與 4.1 一併決定範圍後，在 `check()` 中新增 Rule 4：`app.plugins.*.plugin` 模組不可直接 import `app.core.registry.service_registry`/`app.core.hooks.event_bus`（必須透過參數傳入的 `ctx`）。

**影響檔案**：`scripts/check_architecture_boundaries.py`、`tests/test_architecture_boundaries.py`（可能需要新增針對第 4 條規則的正向/反向測試，比照現有規則的驗證方式）。

### 4.5 熱重載沒有依賴圖感知

**現況問題**：`PluginLoader.reload_one(name)` 只處理 `name` 這一個 plugin：撤銷它的路由/服務/事件訂閱、重新 import、重新 `register()`+`boot()`。它完全不查詢 `plugin.dependencies`，也不查詢「目前有哪些已載入的 plugin 宣告依賴 `name`」。

**為何是落差**：3.3 建立的依賴圖只在**啟動時**的 `_resolve_load_order()` 生效；`reload_one()`（3.12）與依賴圖之間沒有任何互動。目前兩個範例 plugin 之間沒有宣告依賴，所以問題不會在現有測試中暴露，但一旦有人幫 `product_plugin` 宣告依賴 `user_plugin`，然後熱重載 `user_plugin`，`product_plugin` 完全不會被通知——如果 `user_plugin` 重載後對外承諾的能力有變化（例如 `boot()` 這次因為某個暫時性錯誤而失敗，狀態變成 `FAILED`），`product_plugin` 仍會表現得好像 `user_plugin` 一切正常。

**建議做法**：
- `reload_one()` 在撤銷/重建目標 plugin 之後，查詢 `plugin_registry.all()` 找出「`name in plugin.dependencies`」的其他已載入 plugin，並記錄警告（至少寫進 log／回應內容，提示呼叫者「這些 plugin 依賴你剛重載的對象，可能需要一併重載」）。
- 進階可選：提供 `reload_dependents=True` 參數，遞迴（依拓樸順序）重載所有直接/間接依賴者。

**影響檔案**：`app/core/loader.py`（`reload_one`）、`app/api/v1/admin.py`（回應內容可加上 `affected_dependents` 欄位）。

### 4.6 `enabled_plugins` 與 `dependencies` 交互時處理生硬

**現況問題**：`_discover()` 先依 `enabled_plugins` 過濾要 `import` 的 plugin 目錄，再交給 `_resolve_load_order()` 做拓樸排序。如果某個被停用的 plugin 剛好是另一個仍啟用的 plugin 的 `dependencies` 之一，`_resolve_load_order()` 會把這當成「缺少依賴」，丟出 `PluginLoadError`，讓整個 kernel 啟動失敗——即使這個「缺少」完全是設定造成的、可預期的。

**為何是落差**：3.3 對「缺少依賴」的錯誤訊息（`depends on 'X', which was not discovered`）設計初衷是抓**意外**的設定錯誤（拼錯名字、忘記把 plugin 放進資料夾），但沒有區分「因為 `enabled_plugins` 主動排除」跟「純粹找不到」這兩種情況——對維運者來說，錯誤訊息應該要能一眼看出是自己主動關掉的，還是真的壞了。

**建議做法**：在拋出 `PluginLoadError` 前，先檢查該依賴名稱是否存在於 `_PLUGINS_DIR`（只是沒被 `enabled_plugins` selected），錯誤訊息據此分流：「`'X' is disabled via enabled_plugins but required by 'Y'`」vs.「`'X' was not found under app/plugins/`」。是否要進一步允許「自動連鎖停用依賴者」則是一個需要與使用者確認的行為選擇（可能有人希望這仍然是硬錯誤，避免靜默略過重要模組）。

**影響檔案**：`app/core/loader.py`（`_resolve_load_order`/`_discover` 的錯誤訊息)。

### 4.7 `PluginLoader` 對外部注入的支援不對稱

**現況問題**：`PluginLoader.__init__` 的簽章是：

```python
def __init__(
    self, app: FastAPI, *,
    event_bus: EventBus = _default_event_bus,
    settings: Settings = _default_settings,
    service_registry: ServiceRegistry = _default_service_registry,
    db_factory: DatabaseFactory = _default_db_factory,
) -> None:
```

四個 kernel 能力都可以在建構時覆寫，但 `plugin_registry`（`app/core/registry.py` 的另一個模組級單例）卻是在 `load_all()`/`unload_all()`/`reload_one()` 內部直接引用全域 `plugin_registry`，沒有走建構子注入。

**為何是落差**：這是 3.8/3.12 建立的「可注入」慣例沒有覆蓋到最後一個 kernel 單例，測試若想驗證「多個獨立的 `PluginLoader` 互不干擾」（例如平行跑兩個完全隔離的 loader 實例），目前做不到——因為它們永遠共用同一個 `plugin_registry`。

**建議做法**：`PluginLoader.__init__` 增加 `plugin_registry: PluginRegistry = _default_plugin_registry` 參數，內部改用 `self._plugin_registry` 取代裸的 `plugin_registry` 引用。

**影響檔案**：`app/core/loader.py`（改動集中且機械化，簽章對齊其餘四個參數即可）。

### 4.8 Health/introspection 端點沒有列出 facade

**現況問題**：`GET /api/v1/health` 的 `HealthResponse` 只有 `plugins: list[PluginHealth]`，沒有 `facades: list[str]`（或更豐富的 facade 資訊）。

**為何是落差**：3.10 引入 `facade_registry` 的理由之一正是「讓 kernel 有一個地方可以列舉跨 plugin 協調點」，但目前唯一消費 `facade_registry.names()` 的地方只有測試（`tests/test_core.py`），對外的 introspection 端點完全沒有用到它。

**建議做法**：`HealthResponse` 增加 `facades: list[str] = Field(default_factory=lambda: facade_registry.names())` 或等值寫法，讓 `/health` 一次看到「這個 process 裡有哪些 plugin、哪些 facade」。

**影響檔案**：`app/api/v1/health.py`。

### 4.9 沒有 plugin 專屬設定命名空間

**現況問題**：`app/config.py` 的 `Settings` 是所有設定的單一平坦命名空間；`plugin_load_mode`、`enabled_plugins` 這類 kernel 設定，跟未來某個 plugin 想要的（例如 `billing_plugin` 想要一個 `BILLING_INVOICE_PREFIX`）設定，全部會混在同一個 class 裡。

**為何是落差**：這與「plugin 是獨立單元」的精神有一定張力——理論上一個 plugin 被移除時，它專屬的設定也該一起消失，但目前的作法會讓這些設定永久留在 `app/config.py`，隨 plugin 數量增加而膨脹，也讓 kernel 的設定檔對任何一個 plugin 的存在與否產生依賴。

**建議做法**：允許（但不強制）plugin 在自己的 `plugin.py` 或新檔案 `settings.py` 中定義自己的 `BaseSettings` 子類別，並在 `KernelContext` 或建構 `Service` 時各自實例化，而不是集中改 `app/config.py`。這屬於錦上添花的體驗改善，優先度最低，且需要先確認團隊是否接受「設定分散在多處」的取捨（相對於現在「所有設定一目了然」的優點）。

**影響檔案**：無強制修改，屬於未來新增 plugin 時的可選慣例；可在 [plugin-development.md](../../technical/plugin-development.md) 補充一節說明選項。

### 4.10 熱重載未處理 plugin 可能持有的背景資源

**現況問題**：`reload_one()` 目前撤銷的只有「路由」「`service_registry` 發布的能力」「`event_bus` 訂閱」三種資源。如果一個 plugin 在 `boot()` 裡啟動了 `asyncio.create_task(...)` 之類的背景工作、或持有一個需要手動關閉的連線池，`reload_one()` 完全不知道要清理它——舊的背景任務會繼續跑，新的 `boot()` 又啟動一份，造成重複執行或資源洩漏。

**為何是落差**：3.12 的原始設計本來就聚焦在「route/service 邏輯層級」的重載，這點在 `app/core/loader.py` 的 docstring 與 `docs/technical/operations.md` 都已明確寫出限制。但目前這個限制只存在於文件層面，沒有任何**執行期防呆**——一個不熟悉這個限制的 plugin 作者，很容易在 `boot()` 裡起一個背景任務，而重載會悄悄制造出殭屍任務，且不會有任何錯誤訊息。

**建議做法**：
- 短期：在 `AbstractPlugin`/`KernelContext` 文件中，明確建議「若 plugin 需要背景任務，請把 task handle 存在 `self`，並在 `shutdown()` 裡主動 `task.cancel()`」，讓至少遵守慣例的 plugin 能在 `reload_one()` 呼叫 `shutdown()` 時被正確清理（`reload_one()` 已經會呼叫舊實例的 `shutdown()`，只是目前沒有一個「範例」告訴 plugin 作者該怎麼寫）。
- 長期可選：`KernelContext` 提供一個 `spawn_task(coro)` 輔助方法，內部自動用 `owner` 標記追蹤該 plugin 建立的所有 task，`reload_one()`/`unload_all()` 可以呼叫 `cancel_all_from(owner)`，比照 `ServiceRegistry.revoke_all_from`/`EventBus.unsubscribe_all_from` 的模式。

**影響檔案**：`docs/technical/plugin-development.md`（短期）；`app/core/plugin_base.py`、`app/core/loader.py`（長期，若採用 `spawn_task` 方案）。

## 3. 建議導入順序（延續 Phase 1–4 之後的下一輪）

| 階段 | 項目 | 理由 |
|---|---|---|
| Phase 5（貫徹一致性） | 4.1 Service 層納入/明確排除 ctx、4.2 Facade 拿到 ctx、4.4 邊界檢查補第 4 條規則 | 這三項互相牽動同一個決策（Service/Facade 是否也要走 ctx），應該一起定案再動手，避免來回改兩次。 |
| Phase 6（治理收尾） | 4.3 Facade 自動掛載、4.5 熱重載依賴圖感知、4.6 `enabled_plugins`/`dependencies` 錯誤訊息、4.7 `plugin_registry` 注入對齊、4.8 Health 端點列出 facade | 這些互相獨立，可依團隊時間分批處理；4.8 最容易做，可優先當作 quick win。 |
| 暫緩 / 視需求 | 4.9 Plugin 專屬設定命名空間、4.10 熱重載背景資源清理 | 目前兩個範例 plugin 都不需要，屬於「未來有人這樣寫才會踩到的坑」，先在文件寫清楚限制即可，不急著改程式碼。 |

## 3.5 實作 Checklist（優先度「中」與「中低」項目：4.1–4.6）

> 狀態：依使用者指示，只執行落差總覽表中標記 🟠（中）與 🟡（中低）的 6 個項目（4.1–4.6）。🟢（低）的 4.7–4.10 維持規劃現狀、尚未實作。實作前先確認了 4.1/4.2/4.4 牽動的同一個決策：**選擇方案 B——保留現狀（`emit()`/`resolve()` 允許直接用裸單例），只在文件明確寫下取捨，並把邊界檢查新規則限定在 `plugin.py`**，不擴大到 `service.py`/`router.py`（方案 A 需要把 `ctx` 存進 `app.state` 並改兩個 plugin 全部 router 檔案，評估後判斷改動/風險比例不划算）。完成後執行 `pytest -q` → **94 passed**，重跑確認穩定、邊界檢查通過、無殘留檔案。

- [x] **4.1 `KernelContext` 範圍取捨文件化**：`app/core/hooks.py`（`EventBus`）與 `app/core/registry.py`（`ServiceRegistry`）的 docstring 都新增「Scope of the `ctx`-only rule」段落，明確寫下 `subscribe()`/`provide()` 必須走 `ctx`、`emit()`/`resolve()` 允許直接用裸單例的理由與依據（連結本文件 S4.1）。
- [x] **4.2 Facade 不需要 ctx 的理由文件化**：`app/facades/base.py`（`AbstractFacade`）與 `app/facades/catalog_facade.py`（`CatalogFacade`）的 docstring 都補上說明：facade 不是生命週期管理對象、沒有自然的時機建構並傳入 `ctx`，直接 `import service_registry` 呼叫 `resolve()` 是安全且刻意的設計。
- [x] **4.4 邊界檢查新增 Rule 4**：`scripts/check_architecture_boundaries.py` 新增規則：`app.plugins.*.plugin` 模組不可直接 `from app.core.registry import service_registry` 或 `from app.core.hooks import event_bus`（必須透過 `ctx`），但不限制 `service.py`/`router.py`。**已用刻意注入的違規手動驗證過**（正、反案例都測過：`plugin.py` 繞過會被抓、`service.py` 直接用不會被誤判）。`tests/test_architecture_boundaries.py` 新增 8 個測試，用暫存 `tmp_path` 假的 `app/` 目錄樹分別驗證 4 條規則的正面/反面案例。
- [x] **4.3 FacadeRegistry 驅動路由自動掛載**：`FacadeRegistry.register()` 新增可選的 `router` 參數並新增 `routers()` 方法；`app/facades/router.py` 改為在檔案底部呼叫 `facade_registry.register(CatalogFacade, router=router)`（原本在 `catalog_facade.py` 底部的自我註冊已移除，避免循環 import）；新增 `app/facades/__init__.py` 集中 `import app.facades.router` 觸發自我註冊；`app/main.py` 改為 `for facade_router in facade_registry.routers(): app.include_router(...)`，不再手動 import 特定 facade 的 router。已實際啟動 app 驗證掛載結果正確（`mounted routers: ['/catalog']`）。
- [x] **4.5 熱重載依賴圖感知**：`PluginLoader.reload_one()` 回傳型別從 `Result[None, PluginError]` 改為 `Result[list[str], PluginError]`，成功時回傳「目前已載入、且宣告依賴此 plugin」的其他 plugin 名單（不會自動重載它們，僅供呼叫端參考），並寫一筆 warning log。`POST /api/v1/admin/plugins/{name}/reload` 回應從 `204 No Content` 改為 `200` + `{"reloaded": ..., "dependents_may_need_reload": [...]}`。新增測試：暫時 monkeypatch `UserPlugin.dependencies = ["product_plugin"]`，重載 `product_plugin` 後驗證回應正確列出 `user_plugin`。
- [x] **4.6 `enabled_plugins`/`dependencies` 錯誤訊息分流**：`PluginLoader` 新增 `_missing_dependency_message()`，判斷缺少的依賴是否為「磁碟上真實存在、但被 `enabled_plugins` 排除」，若是則給出「... is disabled via enabled_plugins. Add '...' to enabled_plugins, or remove it from '...'.dependencies.」的訊息；否則維持原本「... was not discovered under app/plugins/.」的訊息。新增測試涵蓋兩種情境。

### 3.6 實作 Checklist（優先度「低」項目：4.7–4.10）

> 狀態：4.7–4.10 全部完成。完成後執行 `pytest -q` → **95 passed**，重跑確認穩定、邊界檢查通過、無殘留檔案。

- [x] **4.7 `PluginLoader` 支援注入 `plugin_registry`**：建構子新增 `plugin_registry: PluginRegistry = _default_plugin_registry` 參數，比照既有四個 kernel 能力；內部所有 `plugin_registry.xxx()` 呼叫全面改用 `self._plugin_registry.xxx()`（`load_all`/`_can_proceed`/`_handle_failure`/`unload_all`/`reload_one` 共 16 處）。新增測試 `test_loader_uses_injected_plugin_registry_not_the_global_one`：用假的 `PluginRegistry()` 建構獨立 loader，驗證它完全不觸碰全域單例，兩者互不干擾。
- [x] **4.8 Health 端點列出 facade**：`HealthResponse` 新增 `facades: list[str]`，值來自 `facade_registry.names()`。更新 `tests/test_api.py::test_health_check` 與 `docs/technical/operations.md` 的範例回應。
- [x] **4.9 文件化 Plugin 專屬設定命名空間選項**：`docs/technical/plugin-development.md` 新增「Plugin-specific settings (optional)」一節，說明 plugin 可自訂 `BaseSettings` 子類別而非塞進 `app/config.py` 的慣例與取捨。屬於選用慣例，未強制修改任何程式碼（與本文件原本的建議做法一致）。
- [x] **4.10 文件化熱重載背景任務清理慣例**：`docs/technical/plugin-development.md` 新增「If your plugin starts background tasks」一節，說明 `reload_one()` 不會追蹤背景任務、plugin 作者需自行在 `boot()` 存下 task handle 並在 `shutdown()` 取消。只採用建議做法中的「短期」選項（文件記錄），未實作「長期可選」的 `KernelContext.spawn_task()` 機制——目前兩個範例 plugin 都不需要背景任務，先寫成推測性基礎設施缺乏實際使用場景與測試覆蓋，優先度不高。

至此，本文件第 1 節落差總覽表列出的 **10 個項目（4.1–4.10）全部完成**。

## 4. 明確排除範圍（Non-goals）

- 不討論將 `user_plugin` / `product_plugin` 拆成獨立部署單元、獨立資料庫、獨立 process。
- 不討論跨服務通訊協定（HTTP client、gRPC、Message Queue、Service Mesh）。
- 不討論水平擴展、多實例部署、分散式交易一致性等微服務常見議題。
- 不重複列出 [`docs/spec/done/microkernel-architecture-improvements.md`](./microkernel-architecture-improvements.md) 已完成的 3.1–3.14 項目；本文件只處理該輪實作完成後才浮現的新落差。
- 本文件所有建議都假設**單一 process、單一資料庫**的部署模型不變。
