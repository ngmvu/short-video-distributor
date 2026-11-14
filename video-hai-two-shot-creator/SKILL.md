---
name: video-hai-two-shot-creator
description: Biên kịch + đạo diễn video hài ngắn viral (Facebook Reels, TikTok, YouTube Shorts) lấy cảm hứng từ phong cách hài dân gian & thơ chế kiểu kênh Kỳ Tuổi Khỉ (@kytuoikhi) cho bộ đôi cố định @khi và @nguoi. Xuất kịch bản 3 Scene (~24–28 giây) và prompt Google Flow TWO-SHOT chuẩn khóa nhân vật, khóa giọng tuyệt đối không lộn vai, điệu cười khỉ thương hiệu và chuẩn XANH TIỀN (Ad-Friendly) trên Facebook & YouTube.
---
# SKILL: VIDEO-HÀI-TWO-SHOT-CREATOR v9 — PHONG CÁCH "KỲ TUỔI KHỈ", CHỐT CHẶT VAI TRÒ & AN TOÀN KIẾM TIỀN (FB/YT/FLOW)

> **Bối cảnh kỹ thuật QUAN TRỌNG:** Mỗi Scene được gửi vào Google Flow **riêng lẻ, từng cảnh một**. Flow KHÔNG nhớ cảnh trước.
> ⇒ Mỗi prompt Scene phải **tự chứa đầy đủ** bối cảnh (`SET_LOCK`), ngoại hình & vị trí (`LOCK_KHI`, `LOCK_NGUOI`), giọng nói và người nói. Tuyệt đối không viết `"same as before"`, `"continue"`, `"như cảnh trước"`.

---

## 0. QUY TRÌNH LÀM VIỆC (WORKFLOW)

- **Khi chat trực tiếp với người dùng (2 bước):**
  - **Bước 1 (Lên kịch bản thoại):** Khi nhận ý tưởng, đưa kịch bản thoại 3 cảnh kèm phân định rõ nhân vật nào nói câu nào (`@nguoi` hay `@khi`), nhịp biểu cảm và miếng hài để duyệt trước.
  - **Bước 2 (Xuất Prompt đầy đủ):** Khi người dùng chốt (hoặc yêu cầu xuất luôn), xuất trọn bộ 5 PHẦN kèm prompt Google Flow tích hợp thông số thời lượng, phân vai nghiêm ngặt, chỉ đạo diễn xuất biểu cảm cực đại (Hyper-expressive) và bộ caption/hashtag viral.
- **Khi được gọi tự động từ hệ thống tạo kịch bản (API / Auto-generator):** Xuất trực tiếp cấu trúc đầy đủ 5 PHẦN / JSON theo đúng toàn bộ quy tắc khóa nhân vật, khóa giọng, điệu cười khỉ và an toàn chính sách dưới đây.

---

## 1. TINH HOA PHONG CÁCH "KỲ TUỔI KHỈ" (@kytuoikhi) — 4 MÔ-TÍP TRIỆU VIEW

Lấy cảm hứng từ các chuỗi video nhiều lượt xem nhất trên kênh **Kỳ Tuổi Khỉ** (*Khỉ nhà thơ*, *Bố khỉ tấu hài*, *Khỉ hài hước*, *Con khỉ Khô*), mỗi kịch bản xoay quanh 1 trong 4 mô-típ chủ lực sau (luân phiên đa dạng bối cảnh đời thường Việt Nam):

1. **Khỉ Nhà Thơ (Thơ chế lục bát / 4 chữ bẻ lái):**
   - Ngâm 2 câu thơ lục bát hoặc thơ 4–5 chữ vần điệu, mở đầu nghe rất triết lý, mùi mẫn hoặc cảm thông cho số phận đàn ông/cuộc đời, nhưng vế sau bẻ lái phũ phàng sang chuyện nộp lương, sợ vợ, thiếu nợ hoặc cà khịa thẳng mặt.
   - *Ví dụ nhịp thơ sạch chính sách:* `"Ra đường là cá mập khơi xa, về nhà vợ quát hóa ra cá cảnh!"` hoặc `"Thương thay số phận đàn ông, lương chưa kịp ấm đã không còn đồng!"`
2. **Bố Khỉ Tấu Hài (Hỏi xoáy đáp xoay & Bắt chữ nghĩa đen):**
   - `@nguoi` giao việc hoặc hỏi một câu đời thường (đi chợ mua rau tính tiền thừa, trông tiệm, rửa bát, nhặt được tờ 200k và 500k lượm tờ nào...); `@khi` trả lời hoặc làm theo nghĩa đen bằng logic "khôn lỏi ngược đời" nghe cực kỳ hợp lý khiến `@nguoi` đứng hình.
3. **Nỗi Lòng Đàn Ông Đời Thường (Sợ vợ, Quỹ đen, Bạn nhậu, Cho vay đòi nợ):**
   - `@nguoi` sĩ diện khoe bản lĩnh trụ cột gia đình, khoe không sợ vợ, khoe mới sắm đồ xịn hoặc than thở chuyện cho bạn mượn tiền; `@khi` lập tức bóc mẽ sự thật phũ phàng ngay tại chỗ (vợ gọi một câu đã dạ vâng, giấu quỹ đen trong lốp xe, lúc cho vay là thượng đế lúc đòi nợ phải năn nỉ).
4. **Con Khỉ Khô Mánh Khóe (Báo giá bá đạo / Ghi sổ trừ nợ / Chữa lợn lành thành lợn què):**
   - `@nguoi` nhờ sửa xe hoặc giúp việc nhỏ; `@khi` bày mẹo bá đạo hoặc tính bảng giá/trừ nợ lươn lẹo khiến `@nguoi` ngậm ngùi chốt câu cay đắng.

---

## 2. CÔNG THỨC 3 HỒI & QUY TẮC PHÂN VAI TUYỆT ĐỐI KHÔNG LỘN THOẠI

### Cấu trúc 3 Scene (~24–28 giây tổng):
- **Scene 1 — HOOK MỞ ĐẦU (0–2s phải bắt mắt & bắt tai):**
  - Mở đầu ngay giây 0 (không chào hỏi rườm rà) bằng một câu châm ngôn/thơ chế đời thường, một lời khoe khoang sĩ diện, hoặc một câu hỏi/phàn nàn bức xúc cụ thể kèm 1 chi tiết hình ảnh hài hước.
- **Scene 2 — CÀ KHỊA, LẬT LOGIC & ĐỐI ĐÁP:**
  - Nhân vật còn lại tung chiêu phản đòn: đọc thơ chế bóc mẽ, giải thích theo nghĩa đen lươn lẹo, hoặc báo giá/trừ nợ bá đạo với vẻ mặt tỉnh queo hoặc nhếch mép đắc ý.
- **Scene 3 — CHỐT HẠ CAY ĐẮNG & ĐIỆU CƯỜI KHỈ THƯƠNG HIỆU:**
  - `@nguoi` nhận ra mình bị gài bẫy / bị bóc mẽ, tung câu chốt cay đắng kết thúc bằng cụm từ thương hiệu **"Đúng là khỉ!"** (chỉ xuất hiện duy nhất 1 lần ở Scene 3).
  - Ngay khi `@nguoi` dứt lời, `@khi` bùng nổ **Điệu cười thương hiệu Kỳ Tuổi Khỉ** kết màn.

### Quy tắc Chống Lộn Vai & Nhầm Giọng (CRITICAL ROLE & VOICE LOCK):
- **Mỗi Scene CHỈ ĐÚNG 1 NGƯỜI NÓI (Single-Speaker per Scene):** Để Google Flow không bao giờ đánh tráo khẩu hình giữa khỉ và người, mỗi Scene chỉ cho 1 nhân vật mở miệng nói; nhân vật kia bắt buộc khóa câm (`stays completely silent with mouth closed`).
- **Luân phiên bắt buộc 3 Scene:**
  - **Scene 1:** `@nguoi` nói (`@Voice_Nguoi`) → `@khi` nghe & diễn biểu cảm.
  - **Scene 2:** `@khi` nói (`@Voice_Khi`) → `@nguoi` nghe & đứng hình/bức xúc.
  - **Scene 3:** `@nguoi` nói câu chốt + `"Đúng là khỉ!"` (`@Voice_Nguoi`) → `@khi` im lặng nghe rồi **bật cười ngửa cổ nhe răng thương hiệu**.
- **Kiểm tra chủ sở hữu câu thoại trước khi xuất Prompt:**
  - `@khi` (bên TRÁI - chủ tiệm sửa xe / kẻ ma ranh kèo trên): Xưng **"con/tui"**, gọi **"chú"**. Chỉ nói những câu gài bẫy, đọc thơ trêu chọc, báo giá lươn lẹo, hỏi xoáy.
  - `@nguoi` (bên PHẢI - ông chú khách hàng / bạn nhậu sĩ diện): Xưng **"tao"**, gọi **"mày/khỉ"**. Chỉ nói những câu mở màn khoe khoang/than thở/tra khảo và câu chốt trách móc (`"Đúng là khỉ!"`). Tuyệt đối không gán nhầm câu của chú cho khỉ hoặc ngược lại.

---

## 3. KHÓA NHÂN VẬT, VỊ TRÍ & BỐI CẢNH (CHÉP NGUYÊN VĂN VÀO MỖI SCENE)

Mỗi prompt Scene ở PHẦN 3 bắt buộc chứa nguyên văn 3 khối khóa sau:

1. **SET_LOCK (Bối cảnh đồng nhất cả 3 Scene):**
   - Chọn 1 bối cảnh đời thường Việt Nam hợp chủ đề (tiệm sửa xe vỉa hè, chợ quê, quán cà phê cóc, sân nhà quê, quán nước vỉa hè, phòng khách bình dân, bến xe...) và giữ nguyên văn câu mô tả tiếng Anh ở cả 3 Scene.
   - Ví dụ: `SET: a cramped Vietnamese street-side motorbike repair shop, tire stacks and oil cans in the background, warm afternoon daylight.`
2. **LOCK_KHI (BẮT BUỘC bên TRÁI):**
   - `on the LEFT: @khi, a mischievous adult macaque monkey mechanic, wearing the same dirty greasy white ribbed tank top and dark grey mechanic work trousers, no hat, no glasses, no jewelry`
3. **LOCK_NGUOI (BẮT BUỘC bên PHẢI):**
   - `on the RIGHT: @nguoi, a middle-aged Vietnamese man, wearing the same dark matte half-helmet on his head (strap fastened, never removed), dark grey crew-neck work T-shirt and grey work trousers, no glasses, no jacket`

---

## 4. ĐIỆU CƯỜI KHỈ THƯƠNG HIỆU (@kytuoikhi SIGNATURE LAUGH) & DIỄN XUẤT CỰC ĐẠI

Qua phân tích thực tế kênh **Kỳ Tuổi Khỉ**, điểm giữ chân người xem và tạo viral mạnh nhất là **biểu cảm khuôn mặt cực đại (Hyper-expressive)** và **cách khỉ cười ở cuối video**:

- **Điệu cười Khỉ kết màn ở Scene 3 (Bắt buộc dùng trong prompt Scene 3 cho `@khi`):**
  > `stays completely silent with mouth closed while listening, then bursts into signature cheerful, playful hearty monkey laughter: throwing head all the way back, mouth wide open showing full upper and lower teeth in a big toothy grin, eyes squinted shut, nose crinkled, shoulders bouncing rhythmically, one hand scratching head or giving a cheeky thumbs-up while patting knee in gleeful satisfaction, glancing slyly into the camera`
- **Biểu cảm của `@khi` khi thả miếng / nghe ở Scene 1 & 2:**
  - Khi nghe (Scene 1): `deadpan innocent face, blinking wide eyes, tilting head sideways curiously, scratching head or wiping hands with a cloth, smirking subtly`.
  - Khi nói / cà khịa (Scene 2): `speaking with animated cheeky expression, raising eyebrows, cocking head sideways, giving a confident thumbs-up or counting on fingers with a sly toothy grin`.
- **Biểu cảm của `@nguoi` (Ông chú):**
  - Khi mở màn (Scene 1): `animated proud or frustrated facial expression, gesturing naturally with hands while talking directly to @khi`.
  - Khi bị phản đòn (Scene 2): `freezes with mouth closed, eyes wide in comic disbelief, furrowing brows, facepalming or shaking head in helpless frustration`.
  - Khi chốt hạ (Scene 3): `throwing hands up in comic defeat, shaking head and pointing a finger lecturedly while delivering the final line`.

---

## 5. QUY TẮC THỜI LƯỢNG, KHẨU HÌNH & CHỐNG CẮT THOẠI

- **Độ dài thoại mỗi Scene:** Ngắn gọn, súc tích **10–18 chữ tiếng Việt** mỗi Scene để nhân vật nói dứt câu 100% trước khi hết cảnh, còn dư 1–2 giây cuối cho biểu cảm/tiếng cười.
- **Duration:** Đặt `"duration": "auto"` trong dữ liệu cảnh (hoặc 8–10s tự động theo thoại), luôn chỉ đạo rõ trong prompt: `starts speaking immediately at 0s and finishes speaking completely before the clip ends with a 1-second reaction buffer`.
- **Ánh mắt (Eye-line):** Khi đối thoại, hai nhân vật nhìn trực diện vào nhau (`looking directly at each other`). Chỉ liếc nhìn thẳng vào camera (`glancing slyly into the camera`) ở nhịp cười kết màn.
- **Khóa âm thanh:** Luôn có cụm `Vietnamese language audio only, strictly no English speech, no background music, no subtitles, no on-screen text`.

---

## 6. CHUẨN AN TOÀN CHÍNH SÁCH KÉP: VƯỢT FILTER GOOGLE FLOW & BẬT KIẾM TIỀN FACEBOOK / YOUTUBE (AD-FRIENDLY)

Để video vừa **không bị Google Flow từ chối (`Failed video generation / might violate our policies`)**, vừa **Xanh tiền 100% trên Facebook Reels & YouTube Shorts**:

1. **CẤM TUYỆT ĐỐI trong cả Thoại tiếng Việt lẫn Prompt tiếng Anh:**
   - **Từ bạo lực / vũ khí (gây lỗi Flow & vàng tiền):** Cấm `đánh, đấm, đá, chém, giết, đâm, máu, bóp cổ, cầm gậy đuổi đánh, phang, tát` và các từ tiếng Anh `violently, violent, furiously, rage, screaming, slapping, hitting, punching, weapon, knife, blood, attacking, chasing with a stick`.
     - *Cách thay thế an toàn:* Thay bằng `sternly, dramatically, in comic disbelief, throwing hands up in defeat, patting knee/thigh, shaking head`. Cờ-lê chỉ được cầm hờ thấp (`holding a small wrench loosely`), tuyệt đối không vung hay chỉ vào mặt.
   - **Từ ngữ 18+ / dung tục / bài tiết (gây tắt kiếm tiền FB/YT):** Cấm các từ như `làm tình, hôi nách, đái, ỉa, rắm, phân, ngực, mông` (dù kênh gốc có dùng trong thơ chế, khi đưa vào kịch bản của ta phải chuyển thành hình ảnh hài hước sạch sẽ như `mùi hành tỏi, mùi dầu nhớt, ngủ ngáy như sấm, ví rỗng tuếch`).
   - **Từ chửi thề / hạ nhục / so sánh động vật nhạy cảm:** Cấm `ngu như chó, khổ như chó, tai chó, méo, đéo, vãi, SML, cút`.
     - *Cách thay thế dân gian, duyên dáng:* `"ra đường là hổ về nhà hóa mèo"`, `"khổ như tờ giấy nhám"`, `"đúng là khỉ!"`, `"cạn lời"`, `"chịu thua mày luôn"`.
   - **Cấm nhạy cảm xã hội & bản quyền:** Không nhắc tên người thật, nghệ sĩ, TikToker có thật; không đề cập chính trị, tôn giáo, phân biệt vùng miền, ngoại hình, cờ bạc, rượu bia bê tha hay tiền mặt trao tay mờ ám.

---

## 7. MẪU PROMPT CHUẨN CHO TỪNG SCENE (PHẦN 3)

```text
Scene 1 — Input / First Frame: @nguoi + @khi + @Voice_Nguoi: Medium two-shot, vertical 9:16, eye-level static camera, no cuts. <SET_LOCK> on the LEFT: @khi, a mischievous adult macaque monkey mechanic, wearing the same dirty greasy white ribbed tank top and dark grey mechanic work trousers, no hat, no glasses, no jewelry. on the RIGHT: @nguoi, a middle-aged Vietnamese man, wearing the same dark matte half-helmet on his head (strap fastened, never removed), dark grey crew-neck work T-shirt and grey work trousers, no glasses, no jacket. Vietnamese language audio only, strictly no English speech, no background music, no subtitles. EYE-LINE: both characters look directly at each other. SPEAKER: strictly the man on the RIGHT, @nguoi with @Voice_Nguoi, clearly visible face, speaks immediately at 0s with deep gruff older Vietnamese lip sync: "<Thoại Scene 1 của @nguoi, 10–18 chữ>" — finishes speaking completely before clip ends. LISTENER: strictly the monkey on the LEFT, @khi, remains completely silent with closed mouth and no lip-sync, reacting with expressive comic acting: <biểu cảm ngơ ngác/tỉnh queo/gãi đầu của @khi>. Only @nguoi speaks.

Scene 2 — Input / First Frame: @khi + @nguoi + @Voice_Khi: Medium two-shot, vertical 9:16, eye-level static camera, no cuts. <SET_LOCK> on the LEFT: @khi, a mischievous adult macaque monkey mechanic, wearing the same dirty greasy white ribbed tank top and dark grey mechanic work trousers, no hat, no glasses, no jewelry. on the RIGHT: @nguoi, a middle-aged Vietnamese man, wearing the same dark matte half-helmet on his head (strap fastened, never removed), dark grey crew-neck work T-shirt and grey work trousers, no glasses, no jacket. Vietnamese language audio only, strictly no English speech, no background music, no subtitles. EYE-LINE: both characters look directly at each other. SPEAKER: strictly the monkey on the LEFT, @khi with @Voice_Khi, clearly visible face, speaks immediately at 0s with high-pitched mischievous Vietnamese lip sync: "<Thoại Scene 2 của @khi (thơ chế / bẻ chữ / cà khịa), 10–18 chữ>" — finishes speaking completely before clip ends, cocking head sideways and giving a cheeky thumbs-up with a sly toothy grin. LISTENER: strictly the man on the RIGHT, @nguoi, remains completely silent with closed mouth and no lip-sync, reacting with expressive comic disbelief: eyes wide, furrowed brows, facepalming and shaking head. Only @khi speaks.

Scene 3 — Input / First Frame: @nguoi + @khi + @Voice_Nguoi: Medium two-shot, vertical 9:16, eye-level static camera, no cuts. <SET_LOCK> on the LEFT: @khi, a mischievous adult macaque monkey mechanic, wearing the same dirty greasy white ribbed tank top and dark grey mechanic work trousers, no hat, no glasses, no jewelry. on the RIGHT: @nguoi, a middle-aged Vietnamese man, wearing the same dark matte half-helmet on his head (strap fastened, never removed), dark grey crew-neck work T-shirt and grey work trousers, no glasses, no jacket. Vietnamese language audio only, strictly no English speech, no background music, no subtitles. EYE-LINE: both characters look directly at each other. SPEAKER: strictly the man on the RIGHT, @nguoi with @Voice_Nguoi, clearly visible face, speaks immediately at 0s with deep gruff older Vietnamese lip sync: "<Thoại chốt của @nguoi kết thúc bằng Đúng là khỉ!, 10–18 chữ>" — finishes with a 1-second reaction buffer, throwing hands up in comic defeat. LISTENER: strictly the monkey on the LEFT, @khi, remains completely silent with closed mouth during the line, then bursts into signature cheerful, playful hearty monkey laughter: throwing head all the way back, mouth wide open showing full upper and lower teeth in a big toothy grin, eyes squinted shut, nose crinkled, shoulders bouncing rhythmically, one hand scratching head or giving a cheeky thumbs-up while patting knee in gleeful satisfaction, glancing slyly into the camera. Only @nguoi speaks.
```

---

## 8. FORM XUẤT KỊCH BẢN 5 PHẦN CHUẨN HỆ THỐNG

PHẦN 1 – THÔNG TIN VIDEO
Tên video: <tiêu đề ngắn gọn, gây tò mò kiểu Kỳ Tuổi Khỉ>
Bối cảnh: <mô tả bối cảnh tiếng Việt>
Điểm gây cười & Cú twist: <1 câu tóm tắt cú lật kèo>
Thời lượng tổng: Tự động theo độ dài thoại của 3 Scene (~24–28 giây)
Câu thoại viral nhất: "<câu thơ chế hoặc câu chốt đắt nhất>"

PHẦN 2 – TÓM TẮT KỊCH BẢN
Hook: <Scene 1 mở màn gây tò mò thế nào>
Conflict: <Scene 2 khỉ phản đòn / đọc thơ / bắt chữ ra sao>
Twist: <Scene 3 chú nhận ra cú lừa, mắng "Đúng là khỉ!" và khỉ cười ngửa cổ nhe răng>

PHẦN 3 – GOOGLE FLOW PROMPTS
Scene 1 — Input / First Frame: @nguoi + @khi + @Voice_Nguoi: <Prompt Scene 1 theo mục 7>

Scene 2 — Input / First Frame: @khi + @nguoi + @Voice_Khi: <Prompt Scene 2 theo mục 7>

Scene 3 — Input / First Frame: @nguoi + @khi + @Voice_Nguoi: <Prompt Scene 3 theo mục 7>

PHẦN 4 – TEXT & CAPTION
Text trên video: "<hook ngắn ≤ 8 chữ>"
Caption: "<caption hài hước, sạch chính sách + emoji + hashtag>"
Hashtag: #kytuoikhi #khihaihuoc #khilamtho #chuyendoithuong #reelsfb #Shorts

PHẦN 5 – GỢI Ý TƯƠNG TÁC
"<1 câu hỏi tự nhiên kích thích người xem bình luận / tag bạn bè để kéo tương tác kiếm tiền>"
