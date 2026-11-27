# AAEON 多攝影機錄影系統

本專案在 AAEON Linux 主機上探索區網攝影機，將選定的 RTSP 影像分段存成 MP4，並提供即時預覽與歷史回放。錄影與網站各自以獨立的 systemd 服務運行，關閉瀏覽器或單獨重啟網站時，背景錄影仍會繼續。

## 系統架構

```text
瀏覽器 ── HTTP ──► web_viewer.py ── 載入 ──► web/ 前端頁面
                      │
                      ├─ 寫入 record_config.json ──► record_cams.py 讀取錄影名單
                      ├─ 讀取 record_status.json ◄── record_cams.py 寫入錄影狀態
                      ├─ 即時預覽 ──► FFmpeg 另行連接攝影機
                      └─ 歷史回放 ──► 讀取已儲存的 MP4

攝影機 ── RTSP ──► 每台一個 FFmpeg 錄影程序 ──► 攝影機目錄 / 日期_時間.mp4
                       ▲
                 record_cams.py 管理啟停、排程與重連
```

### 程式與檔案分工

| 檔案 | 職責 |
|---|---|
| [discover.py](discover.py) | 找出本機 IPv4 網段，透過 ONVIF 探索裝置，再測試 RTSP 連接埠與串流路徑。錄影與網站共用這份探測程式，也可單獨執行。 |
| [scan_store.py](scan_store.py) | 共用掃描鎖及最近一次完成的掃描結果，避免錄影服務與網站同時掃描同一網段。 |
| [record_config.py](record_config.py) | 兩個服務共用的設定模組，管理路徑、預設值、欄位驗證及 JSON 原子寫入。 |
| [record_config.example.json](record_config.example.json) | 公開範本。實際錄影名單存在本機的 `record_config.json`。包含全域暫停旗標 `paused`，以及各攝影機的 `ip` 和 `url`。 |
| [record_cams.py](record_cams.py) | 錄影服務入口。監看網段與設定、管理每台攝影機的 FFmpeg、確認檔案持續寫入，並在低空間時清理舊片段。 |
| [recording_health.py](recording_health.py) | 將錄影設定、服務回報與檔案成長轉成每台攝影機的顯示狀態，支援舊版及缺少心跳的錄影器。 |
| [recording_archive.py](recording_archive.py) | 列出攝影機的 MP4，核對片段結構及寫入狀態，以攝影機目錄與檔案身分驗證回放、下載和刪除目標。 |
| [web_viewer.py](web_viewer.py) | HTTP 服務入口。提供靜態資源、攝影機與錄影控制 API、即時串流、MP4 分段讀取、下載與刪除。 |
| [web/index.html](web/index.html)、[web/styles.css](web/styles.css)、[web/app.js](web/app.js) | 分別負責頁面結構、版面樣式，以及預覽、錄影按鈕與回放互動。使用原生瀏覽器 API，部署時直接複製即可。 |
| [record_cams.service](record_cams.service)、[web_viewer.service](web_viewer.service)、[install_service.sh](install_service.sh) | 提供兩份 systemd 範本，安裝時帶入本機服務帳號與專案路徑，並提供個別或整組安裝、重啟與狀態檢查。 |
| `test_*.py`、[check_viewer_browser.py](check_viewer_browser.py) | 標準函式庫測試涵蓋掃描、錄影、設定與檔案保護。瀏覽器測試另用產生的 MP4 驗證桌面與手機操作。 |

## 從發現攝影機到回放

1. **探索攝影機**。錄影服務啟動時掃描，每 5 秒檢查有效網段，換網段時重掃，平時每 10 分鐘補掃。網站讀取共用的 `recorder_discovered.json`，升級時可讀取舊的 `discovered.json`。需要立即重搜時可按「重新掃描」，錄影服務關閉時也可使用。
2. **選機**：使用者在網頁開啟某台錄影後，網站更新 `record_config.json`。錄影服務約每 2 秒重新讀取，依選擇啟停對應工作。新發現的攝影機須先選取才會錄影。若啟動錄影程式時直接傳入 IP，該次執行使用命令列名單。
3. **錄影**：預設依主機本地時間每天 09:00–17:00 錄影，以 RTSP over TCP 接收影像，用 FFmpeg 直接複製影像軌，每 600 秒切一段 MP4。解析度與編碼品質由攝影機串流決定。以 `192.0.2.10` 為例，輸出路徑形如 `192_0_2_10/20261126_091000.mp4`。
4. **監測與保留**：錄影服務依片段寫入進度更新 `record_status.json`。單台連續 30 秒無進展時，會收尾並重連該台。磁碟剩餘低於預設 5 GB 時，依修改時間清理舊片段；無法確認某台正在寫入哪個檔案時，本輪略過該台。
5. **觀看與取檔**。進入「架設檢查」確認寫入，在「錄影檔」依攝影機及日期回放、下載或取得主機資料夾路徑。

## 網頁操作

多台一起錄影時，在「架設檢查」勾選待啟用鏡頭旁的方框，再按「啟用所選」。整批會一次加入錄影名單，其他已啟用的攝影機會繼續錄製。若全機暫停，可從「錄影管理」恢復。錄影服務約每 2 秒讀取設定，並在排程時段內為每台攝影機啟動獨立的 FFmpeg，分別儲存 MP4。各路串流的連線與首幀時間可能不同，可從每列的「寫入中」確認進度。

- **到場架設**。首頁每 5 秒更新一次。每台攝影機分開顯示是否啟用、目前狀態及最後寫入時間。「寫入中」須有同一個 MP4 檔案變大的證據，有效期為 15 秒。首次看到檔案、換檔及重連時都重新取樣。缺少近期服務回報時，網站會比較前後取樣並標示證據來源。
- **看即時畫面**。點攝影機旁的「預覽」才開串流，每個頁面最多四路。支援的瀏覽器直接播放重新封裝的 H.264，其餘使用 MJPEG。切到「錄影檔」或讓分頁進入背景會關閉預覽。
- **回來取檔**。「錄影檔」預設顯示主機今天與前兩天，包含已停錄鏡頭。每一天列出已完成段數、總容量及首末片段起錄時間。展開日期可回放或下載單段。整批拷貝時，可用「複製路徑」找到主機上的攝影機目錄，依清單挑選已完成的檔案。清單可手動重新整理。
- **刪除**。開啟「管理檔案」後可刪單段或當天已完成的片段。確認視窗列出攝影機、檔名或日期、段數和容量。伺服器會重新比對檔案身分、MP4 結構及是否仍開啟寫入。Linux 使用檔案租約，無法確認或片段已被替換時會保留檔案並回報原因。升級前已開著的網頁須重新整理才能刪除。

## 安裝時要填哪些路徑

正式主機需要 Linux、Python 3.6 以上、FFmpeg、`ip`（iproute2），安裝服務時另需 systemd。網站預設使用 8080 埠。

| 項目 | 如何設定 |
|---|---|
| 專案目錄與錄影磁碟 | 把完整專案放在**已掛載的錄影磁碟**上。安裝腳本從自身位置取得絕對路徑，填入兩個服務的 `WorkingDirectory` 與 `RequiresMountsFor`。開機使用外接碟時，先設定 Linux 的持久掛載點。 |
| 服務帳號 | 預設使用執行 `sudo` 前的登入帳號。若要另外指定，使用 `sudo SERVICE_USER=實際帳號 bash install_service.sh all`。該帳號須能讀取程式與 `web/`，並能在專案目錄建立與更新檔案。 |
| Python 路徑 | 兩份 `.service` 範本預設使用 `/usr/bin/python3`。若要使用不同的 Python，將兩份範本的 `ExecStart` 第一個欄位改為該直譯器的絕對路徑，再執行安裝腳本。 |
| 攝影機與 RTSP 路徑 | 全新部署會在專案目錄建立空白的 `record_config.json`。可在網站掃描並選取鏡頭，或在本機設定每台的 `ip` 和 `url`。沿用舊部署時，把原本的設定放在此處即可。 |
| 影片與回放路徑 | 預設每台輸出至 `專案目錄/IP_以底線分隔/日期_時間.mp4`，網站從同一目錄讀取。若單獨使用錄影器的 `--out`，只有影片輸出與磁碟清理改用該路徑，網站仍讀取專案目錄。 |

### 手動測試

先進入放置程式的目錄。以下兩個指令分別在兩個終端機執行；已有正式網站時，8081 可供測試。

```bash
python3 -B web_viewer.py --port 8081
```

```bash
python3 -B record_cams.py --always
```

在瀏覽器開啟 `http://主機IP:8081/`，選取鏡頭後確認各台顯示「寫入中」。與正式環境並行測試時，請使用另一份專案目錄，使錄影檔、設定和狀態彼此獨立。使用 Ctrl+C 可讓手動錄影器收尾。

### 安裝 systemd 服務

在正式部署目錄執行安裝腳本。它會產生適用於本機的 unit、安裝到 `/etc/systemd/system/`、設為開機啟動，並**立即重啟**選定服務。移動專案或修改範本後，重新執行安裝腳本。

```bash
sudo bash install_service.sh all
systemctl show -p Id -p ActiveState -p WorkingDirectory -p ExecStart record_cams.service web_viewer.service
```

腳本也接受 `record_cams` 或 `web_viewer`，可單獨更新其中一個服務。

### 驗證

```bash
python3 -B -m unittest discover -v
```

選用的瀏覽器測試為 `python3 -B check_viewer_browser.py`，需安裝 Python Playwright、Chromium 或 Chrome，以及支援 libx264 的 FFmpeg。如需指定瀏覽器，請將 `CHROME_BIN` 設為本機瀏覽器執行檔的完整路徑。選用的 `VIEWER_SCREENSHOTS` 可指向 `screenshots/` 儲存測試畫面。
