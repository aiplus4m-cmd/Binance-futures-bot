# Binance Futures Bot

Bot giao dịch USDⓈ-M Futures trên Binance, điều khiển và báo cáo qua Telegram.

> ⚠️ Giao dịch futures có đòn bẩy rủi ro rất cao. Hãy chạy **testnet** (`BINANCE_TESTNET=true`) vài tuần
> và chỉ dùng số vốn bạn chấp nhận mất. Không có chiến lược nào đảm bảo lợi nhuận.

## Cài đặt

```bash
pip install -r requirements.txt
cp .env.example .env      # điền 5 biến
python -m bot
```

Hoặc Docker: `docker compose up -d --build` (dữ liệu nằm trong `./data`).

### `.env` — chỉ 5 biến

| Biến | Ý nghĩa |
|---|---|
| `BINANCE_API_KEY`, `BINANCE_API_SECRET` | API key bật quyền **Futures**. KHÔNG bật quyền rút tiền. Nên giới hạn IP. |
| `TELEGRAM_BOT_TOKEN` | Token từ @BotFather |
| `TELEGRAM_CHAT_ID` | Chat ID của bạn (lấy từ @userinfobot). Bot chỉ nhận lệnh từ chat này. |
| `BINANCE_TESTNET` | `true` = testnet, `false` = tiền thật |

Mọi tham số chiến lược/rủi ro là hằng số có chú thích trong [`bot/config.py`](bot/config.py).

## Theo dõi vốn

- **Lần chạy đầu tiên**: số dư ví Futures (`totalWalletBalance`) được ghi làm **vốn ban đầu**.
- **Nạp / rút**: bot đọc lịch sử `income` của Binance mỗi 5 phút; mọi khoản chuyển vào/ra ví Futures
  (`TRANSFER`) được ghi lại và báo lên Telegram.
- **Vốn đầu tư ròng** = vốn ban đầu + nạp − rút; **Lãi/lỗ** = equity hiện tại − vốn đầu tư ròng.
  Nhờ vậy nạp/rút không làm sai lệch ROI.
- PnL đã chốt, phí, funding được lưu theo từng bản ghi → báo cáo theo ngày (múi giờ `Asia/Ho_Chi_Minh`).
  Báo cáo ngày hôm trước được gửi tự động sau 0h.

## Lệnh Telegram

| Lệnh | |
|---|---|
| `/status` | Equity, vốn ròng, lãi/lỗ tổng + hôm nay, số vị thế |
| `/positions` | Vị thế đang mở: giá TB, SL, TP1/TP, DCA, giá thanh lý |
| `/history [n]` | n lệnh đã đóng gần nhất |
| `/trade <id>` | Chi tiết + diễn biến 1 lệnh (mở, TP1, trailing, DCA, đóng) |
| `/report [YYYY-MM-DD]` | Báo cáo 1 ngày |
| `/days [n]` | Bảng tổng hợp n ngày |
| `/capital` | Vốn ban đầu, nạp/rút, ROI |
| `/scan` | Quét tín hiệu ngay (chỉ xem) |
| `/pause`, `/resume` | Dừng / tiếp tục mở lệnh mới |
| `/close SYMBOL [force]` | Đóng vị thế; nếu đang lỗ phải thêm `force` |

## Chiến lược

**Chọn vị thế** (mỗi khi nến 15m đóng):
1. Top 40 cặp USDT perpetual theo volume (≥ 50M USDT/24h), bỏ coin mới niêm yết < 30 ngày, bỏ coin biến động 24h > 25%.
2. Xu hướng đồng thuận 4h + 1h (EMA20/EMA50), ADX(1h) ≥ 20, giá không quá xa EMA20 1h.
3. Điểm vào 15m: giá hồi về EMA20 rồi bật lại (RSI cắt lên/xuống 50, nến đóng qua EMA20).
4. Chấm điểm 0–100 (sức mạnh trend, volume, khoảng trống tới kháng cự/hỗ trợ, funding, RSI 1h).
   Chỉ vào lệnh tốt nhất đạt ≥ 60 điểm. Bỏ long khi BTC đang sập mạnh (và ngược lại).

**TP / SL / khối lượng / đòn bẩy** (`bot/risk.py`):
- SL: sau đáy/đỉnh swing 15m + 0.25 ATR, tối thiểu 1.2 ATR(1h); cấu trúc quá xa (> 2.5 ATR) → bỏ qua.
- TP: ≥ 1.8 × khoảng SL (R:R ≥ 1.8) và ≥ 2 ATR.
- Khối lượng: sao cho chạm TP lãi ròng (sau phí) **≈ 2% vốn**; nếu vì giới hạn rủi ro/ký quỹ mà
  kỳ vọng < 1.5% vốn → không vào lệnh.
- Rủi ro chạm SL ≤ 1.2% vốn cho lệnh đầu; tổng cả DCA ≤ 1.5% vốn.
- Đòn bẩy: margin isolated; chọn đòn bẩy thấp nhất đủ để ký quỹ ≤ 25% vốn, và luôn đảm bảo
  giá thanh lý cách giá vào ≥ 3 lần khoảng SL (SL luôn khớp trước thanh lý). Tối đa 20x.

**Quản lý vị thế** (mỗi 15 giây):
- SL và TP đặt sẵn trên sàn (STOP_MARKET / TAKE_PROFIT_MARKET, close-position) → vẫn an toàn khi bot tắt.
- **TP1** (50% quãng đường tới TP): chốt 40% khối lượng, **dời SL về hòa vốn + phí** theo giá trung bình.
  Chốt TP1 + TP ≈ 1.6% vốn; từ đây phần còn lại không thể lỗ.
- Sau TP1: **trailing SL** theo 1.5 ATR từ đỉnh/đáy, không bao giờ thấp hơn hòa vốn.
- **DCA** (tối đa 1 lần): khi giá đi 45–80% quãng tới SL *và* xu hướng 4h/1h còn nguyên *và* nến 15m
  xác nhận bật lại → nhồi thêm (≤ 60% lệnh đầu, trong ngân sách rủi ro 1.5%). Tính lại **giá trung bình**,
  TP/TP1 mới (cùng mục tiêu USDT → TP gần hơn). **SL giữ nguyên**, không bao giờ nới SL.
- **Không đóng khi PnL < 0**: mọi lệnh đóng do bot chủ động (time-exit, đảo trend, /close) đều kiểm tra
  PnL ròng (đã chốt + chưa chốt − phí − funding − phí đóng) > 0. Lệnh lỗ chỉ đóng bởi SL.
- **Tránh giam vốn**: sau 12h, mức lãi cần để đóng giảm dần từ 80% mục tiêu xuống 0.1% vốn ở giờ thứ 48;
  sau 48h đóng ngay khi lãi ròng dương. Nếu xu hướng 1h đảo chiều và đang lãi → chốt.
- Tự đặt lại SL/TP nếu lệnh trên sàn bị mất; sau SL, không vào lại symbol đó trong 6h.
- Lỗ trong ngày ≥ 4% → ngừng mở lệnh mới tới hết ngày. Tối đa 3 vị thế cùng lúc.

## Cấu trúc

```
bot/
  config.py          hằng số chiến lược + đọc .env
  binance_client.py  REST client (ký HMAC, làm tròn tick/step, Algo Order API cho SL/TP)
  strategy.py        quét & chấm điểm tín hiệu
  risk.py            tính khối lượng, đòn bẩy, TP/SL, DCA, hòa vốn, trailing, time-exit (thuần toán)
  trader.py          vào lệnh, quản lý, đóng lệnh
  accounting.py      vốn ban đầu, nạp/rút, báo cáo
  telegram.py, app.py
tests/               unit test + mô phỏng vòng đời lệnh với sàn giả lập
```

Chạy test: `pip install -r requirements-dev.txt && pytest -q`

## Lưu ý

- Tài khoản phải ở **One-way mode** (bot tự chuyển nếu chưa có vị thế). Vị thế bạn mở tay được bỏ qua
  (hiển thị là "ngoài bot") và bot không mở lệnh trên symbol đó.
- Từ 12/2025 Binance chuyển lệnh điều kiện sang Algo Order API (`/fapi/v1/algoOrder`); client dùng API này và
  tự lùi về `/fapi/v1/order` nếu môi trường chưa hỗ trợ.
- Dữ liệu (SQLite + log) ở `data/`. Xóa `data/bot.db` = bắt đầu lại từ đầu với vốn ban đầu mới.
