# Multi-Source Raw Log UDP 發送模擬器

這是一個跨平台（支援 macOS / Linux，並相容 Windows）的桌面圖形化 Log 發送工具，專為 SIEM / Log Collector 模擬多系統來源日誌測試所設計。

---

## 系統需求與 Python 版本建議

- **作業系統**：macOS (Sonoma / Sequoia 等)、Linux (Ubuntu / Debian / CentOS / RHEL 等)、Windows 10/11
- **Python 版本**：**Python >= 3.9**（最佳推薦 **Python 3.10 ~ 3.12**）
- **依賴套件**：僅需 `PySide6`（Qt6 官方套件）。底層網路發送、Raw Socket 封包構造、網卡 IP Alias 管理等均採用 Python 內建標準函式庫，輕量且無多餘依賴。

---

## 核心功能

1. **多系統 Raw Log 檔案管理**：每個 Raw Log 檔案代表一個獨立系統，支援單檔/多檔選取或整個資料夾匯入，預設附帶 4 個不同系統的範例 Raw Log（Palo Alto 防火牆、Linux SSHD、Windows Security Event、Nginx Web）。
2. **多執行緒同時並發發送**：勾選的所有 Raw Log 檔案會由獨立執行緒**同時並發發送**，互不阻塞，並提供每個任務獨立的即時進度條與輪數統計。
3. **每個 Raw Log 獨立模擬不同來源 IP (Source IP)**：
   - 支援 **網卡虛擬副 IP 綁定模式 (IP Alias + socket.bind)**（推薦）：自動在網卡掛載副 IP 並真實綁定發送，100% 通過實體交換器與系統路由，測試完一鍵清除或關閉自動卸載。
   - 支援 **Raw Socket 封包層偽造（需 `sudo` 權限）**：直接自訂封裝 IPv4 + UDP Header，不需修改本機網卡設定即可模擬任意跨網段來源 IP。
   - 支援 **一般 UDP Socket 備援模式（免 `sudo` 權限）**：一般權限下可直接使用本機預設 IP 進行快速流程與接收測試。
   - 內建 **「🔢 批次自動遞增分配來源 IP」** 工具：自動偵測目前網卡同網段 IP，一鍵為清單內所有檔案分配不重複的來源 IP。
4. **預設 UDP 514 並支援自訂 Port**：全域預設為 `UDP 514`，且列表中的每個檔案皆可個別覆寫目標 Port（如 `1514`、`5514`）。
5. **支援重複發送與速率控制 (EPS)**：
   - 可設定重複發送 `1 ~ N` 次，或勾選**無限循環發送**直到手動停止。
   - 支援設定每輪間隔秒數，以及每秒發送筆數上限（EPS），每個檔案亦可個別覆寫。
6. **設定檔存取與內建測試接收器**：
   - 支援匯出/載入 `.json` 設定檔（預設自動載入 `default_profile.json`）。
   - 下方分頁內建 **「📡 本機 UDP 測試接收器」**，無需外部 SIEM 即可在本機自發自收驗證。

---

## 快速啟動方式（首次執行會自動建立虛擬環境）

首次在全新環境或複製專案後執行，腳本會**自動檢查 Python 版本、自動建立 `.venv` 虛擬環境，並自動安裝必要套件**，無需手動執行 `pip install`！

### 方式 A：以 `sudo` 啟動（推薦：完整啟用「網卡 IP Alias 掛載」與「Raw Socket 模擬其他來源 IP」）

在終端機執行：

```bash
./run_sudo.sh
```

### 方式 B：一般權限啟動（免密碼，使用一般 UDP Socket 備援模式）

在終端機執行：

```bash
./run.sh
```

### 方式 C：直接使用 Python 啟動

程式內建環境自動重導，若直接執行 `main.py` 也會自動轉向 `.venv` 執行：

```bash
./main.py
# 或以管理員權限
sudo ./main.py
```
