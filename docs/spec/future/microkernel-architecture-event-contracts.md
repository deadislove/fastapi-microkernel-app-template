# Microkernel 架構強化建議（第四輪：誠實的現況評估）

> 範圍聲明：本文件僅討論**單體部署（in-process）下的 Microkernel 架構純度**，不涉及微服務情境。延續 [`docs/spec/done/microkernel-architecture-improvements.md`](../done/microkernel-architecture-improvements.md)（3.1–3.14）、[`docs/spec/done/microkernel-architecture-refinements.md`](../done/microkernel-architecture-refinements.md)（4.1–4.10）、[`docs/spec/done/microkernel-architecture-observability.md`](../done/microkernel-architecture-observability.md)（5.1–5.5）的方向。

## 0. 先說結論：這一輪找到的東西明顯變少了

前三輪總共 29 個項目（3.1–3.14、4.1–4.10、5.1–5.5）已全部實作完成並通過全面測試。這次重新檢視整個 `app/` 目錄、`scripts/`、`docs/technical/`，**用實際 grep/程式碼驗證**（不是憑印象）找了一輪，誠實地說：**能找到的落差已經非常薄，而且都是文件/一致性層級的小事，沒有結構性問題**。

這代表對照 Microkernel Architecture 的核心理念——kernel 極簡、plugin 動態發現、跨模組只透過 kernel 仲介的通道溝通、kernel 不依賴任何 plugin、可觀察性、容錯隔離、契約版本化——這個專案目前已經是相當完整、有測試覆蓋的教科書等級實作。**如果你的目標就是「達成 Microkernel Architecture 理念」，這個目標基本上已經達成了。**

以下記錄本輪找到的 2 個小落差，供你參考是否要繼續做；但也建議你考慮：**繼續用同一個提示詞跑第五輪，報酬可能會越來越低**——如果還有具體想解決的問題（例如效能、安全性、實際新功能、或終於要不要 `git init` 接上版本控制），直接告訴我會比再跑一次通用的架構掃描更有效率。

## 1. 落差總覽

| 項目 | 現況 | 落差程度 |
|---|---|---|
| Event 沒有 payload 契約/慣例文件 | `product.created`（`product_id`, `sku`）、`user.created`（`user_id`, `email`）都是直接用關鍵字參數 emit，沒有 schema，訂閱端目前用「預設值 + `**_` 收尾」的防禦寫法，但這個寫法**只存在於範例程式碼**，`plugin-development.md` 沒有把它寫成建議慣例 | 🟢 低 |
| Scaffold 工具沒有展示 `version` 欄位 | `scripts/new_plugin.py` 產生的 `plugin.py` 有註解示範 `dependencies`，但 5.4 新增的 `version`（純資訊性版本號）完全沒有出現在產生的程式碼或範例裡 | 🟢 低 |

## 2. 詳細項目

### 6.1 Event 沒有 payload 契約/慣例文件

**現況問題**：`app/plugins/product_plugin/service.py` 用 `event_bus.emit("product.created", product_id=product.id, sku=product.sku)`；`app/plugins/user_plugin/service.py` 用 `event_bus.emit("user.created", user_id=user.id, email=user.email)`。訂閱端（`user_plugin/plugin.py` 的 `_log_product_created`）簽章是 `async def _log_product_created(product_id: str = "", sku: str = "", **_: object)`——用預設值加 `**_` catch-all，這樣即使發送端未來加欄位或訂閱端漏接某個欄位都不會直接炸掉。但這只是**這個範例剛好寫成這樣**，`docs/technical/plugin-development.md` 的「Reacting to another plugin's event」一節只示範了怎麼訂閱，沒有把「訂閱端應該用預設值+`**_`防禦」寫成建議慣例，也沒有提到「event 名稱建議用 `resource.past_tense_verb` 格式」這種現有慣例。

**為何是落差**：這不是安全問題（`EventBus.emit`/`subscribe` 機制本身沒有問題），純粹是「文件沒有把已經在用的好習慣寫下來」——下一個寫 plugin 的人如果沒看過 `user_plugin` 的原始碼，可能會寫一個沒有預設值、沒有 `**_` 的訂閱函式，一旦發送端未來調整欄位就會在 runtime 直接丟 `TypeError`。

**建議做法**：在 `docs/technical/plugin-development.md` 的「Reacting to another plugin's event」小節補兩句話：(1) event 名稱建議用 `resource.past_tense_verb`（如 `product.created`）；(2) 訂閱函式建議所有參數給預設值、並加 `**_: object` 收尾，讓發送端未來新增欄位、或訂閱端只關心其中幾個欄位時都不會壞掉。純文件，不改程式碼行為。

**影響檔案**：`docs/technical/plugin-development.md`。

### 6.2 Scaffold 工具沒有展示 `version` 欄位

**現況問題**：`scripts/new_plugin.py` 產生的 `plugin.py` 範本裡有 `# dependencies = ["user_plugin"]` 這樣的註解提示，但 5.4 新增的 `version: ClassVar[str] = "0.0.0"` 完全沒有出現——這是因為 scaffold 工具是在 3.14（第一輪）就寫好的，而 `version` 欄位是後來 5.4（第三輪）才加進 `AbstractPlugin` 契約，兩者沒有同步更新。

**為何是落差**：不影響功能（`version` 有預設值，新 plugin 不設定也能正常運作），純粹是「scaffold 產出的範本」跟「`AbstractPlugin` 完整契約」出現落差——用 scaffold 工具的人不會知道還有這個可選欄位可以用。

**建議做法**：在 `scripts/new_plugin.py` 的 `plugin_py` 範本裡，比照 `dependencies` 的做法加一行註解提示，例如 `# version = "1.0.0"  # optional — shown by GET /api/v1/health`。

**影響檔案**：`scripts/new_plugin.py`（`tests/test_scaffold.py` 的既有測試不需要改，因為只是新增註解，不影響產生的程式碼是否可編譯/符合契約）。

## 3. 建議導入順序

兩項都是文件/範本層級的小補丁，沒有相依性，做的話直接一起做即可；不做也完全不影響現有功能。

## 4. 明確排除範圍（Non-goals）

- 不討論將 `user_plugin` / `product_plugin` 拆成獨立部署單元、獨立資料庫、獨立 process。
- 不討論跨服務通訊協定、水平擴展、多實例部署等微服務常見議題。
- 不重複列出前三輪已完成的 29 個項目。
- 本文件刻意保持簡短——這是誠實反映「這一輪真的沒找到多少東西」，不是為了填內容而灌水。
