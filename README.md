# Runpod Anime Subber (ترجمة ← حرق ← ضغط MP4 ← رفع سحابي)

أداة بواجهة ويب (**Gradio**) للتشغيل على **Runpod**:

- **التدفق:** الحلقة بترجمتها الإنجليزية المدمجة (بتتسحب تلقائياً من الـ MKV) → ترجمة عربية بأسلوب Celestial-Dragons عبر **DeepSeek** (`deepseek-v4-pro`) → حرق الترجمة → ضغط **HEVC (NVENC)** بأعلى جودة وأقل حجم → ناتج **MP4** جاهز.
- **وضع المواسم الكاملة (جديد):** الصق رابط تورنت أو أكثر (رابط في كل سطر، والرابط الواحد ممكن يحتوي موسماً كاملاً أو أكثر) والأداة هتحمّل كل الحلقات وترتبها برقم الحلقة تلقائياً، وتترجم وتحرق وتضغط كل حلقة ورا التانية، وتجمع الناتج في **فولدر واحد باسم الأنمي** — ولو الأنمي طويل (أكتر من 35 حلقة) يتقسم تلقائياً لفولدرات **35 حلقة** (`Part 01 (EP001-EP035)` وهكذا). الاستكمال تلقائي: الحلقات الجاهزة بتتخطى، ونقاط حفظ الترجمة محفوظة لكل حلقة.
- **شكل الترجمة:** نفس ستايل مترجم Celestial-Dragons بالظبط (الفونتات والمقاسات والألوان والأماكن من ملفات الحلقات الأصلية)، مع بدائل تلقائية للفونتات الناقصة — وأي فونت جديد تحطه في فولدر `fonts/` بيشتغل لوحده من غير تعديل كود.
- **الحلقات الخام بدون أي ترجمة:** **Faster-Whisper** يسمع الصوت الياباني ويفرّغه نصاً تلقائياً، وبعدين نفس خط الترجمة والحرق والضغط.
- إدخال روابط أو ملفات تورنت منفصلة لكل جودة (**1080p** / **720p** / **480p**) وحفظ دائم في `/workspace/raw_episodes`.
- رفع النتائج على **DoodStream** و **Google Drive** واستخراج الروابط.
- المفاتيح (DeepSeek / DoodStream) محفوظة في `subber_secrets.json` في الريبو وبتتقري تلقائياً عند التشغيل.

## التشغيل (ترمنال Jupyter على Runpod)
الصق الأمر ده في الترمنال وخلاص:

```
apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y ffmpeg aria2 curl unzip fontconfig rclone xvfb fluxbox x11vnc novnc websockify && wget -q -O /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb && apt-get install -y /tmp/chrome.deb && rm -f /tmp/chrome.deb && mkdir -p /workspace/models /workspace/glossaries /workspace/outputs /workspace/raw_episodes /root/.vnc && cd /workspace && ( [ -d runpod-anime-subber ] && (cd runpod-anime-subber && git fetch origin && git reset --hard origin/main) || git clone https://github.com/ismailhesham/runpod-anime-subber.git ) && cd /workspace/runpod-anime-subber && mkdir -p /usr/local/share/fonts/anime_subber && cp -f fonts/* /usr/local/share/fonts/anime_subber/ && fc-cache -fv && ln -sf /usr/share/novnc/vnc.html /usr/share/novnc/index.html && x11vnc -storepasswd 123456 /root/.vnc/passwd >/dev/null && (nohup Xvfb :99 -screen 0 1280x800x24 >/dev/null 2>&1 < /dev/null &) && sleep 1 && (DISPLAY=:99 nohup fluxbox >/dev/null 2>&1 < /dev/null &) && (nohup x11vnc -display :99 -rfbauth /root/.vnc/passwd -listen 127.0.0.1 -rfbport 5900 -forever -shared >/dev/null 2>&1 < /dev/null &) && (nohup websockify --web=/usr/share/novnc 0.0.0.0:6080 127.0.0.1:5900 >/dev/null 2>&1 < /dev/null &) && (DISPLAY=:99 nohup google-chrome --no-sandbox --disable-dev-shm-usage --user-data-dir=/workspace/chrome_profile https://www.yourupload.com https://videa.hu >/dev/null 2>&1 < /dev/null &) && pip install -r requirements.txt && (GRADIO_SHARE=false PORT=7860 UI_USER=admin UI_PASS=123456 RUNPOD_OUTPUT_ROOT=/workspace/outputs nohup python3 /workspace/runpod-anime-subber/app.py > /workspace/app.log 2>&1 < /dev/null &) && sleep 8 && tail -n 5 /workspace/app.log && echo "READY - open RunPod Connect > HTTP Service on port 7860"
```

بعدها افتح الواجهة من RunPod: **Connect ← HTTP Service (بورت 7860)**، وسجّل دخول بـ admin / 123456.
متصفح Chrome للرفع اليدوي على yourupload/videa: **Connect ← HTTP Service (بورت 6080)**.

## وضع المواسم (الاستخدام الأساسي الجديد)
1. افتح الواجهة، وفي قسم **«وضع المواسم الكاملة»** الصق روابط التورنت/الماجنت (رابط في كل سطر).
2. اكتب اسم الأنمي في خانة «اسم الأنمي» (بيستخدم لتسمية الفولدر وتوحيد قاموس الأسماء).
3. اضغط **SEASON STRIKE** وسيبه يشتغل: تحميل ← ترجمة ← حرق ← ضغط لكل الحلقات بالترتيب.
4. الناتج في `/workspace/outputs/<اسم الأنمي>/` (مع تقسيم ×35 للأنمي الطويل)، وملفات الترجمة `.ass` في فولدر `_ass_files` جنبها.
5. بعد ما يخلص اضغط **S-PORTAL** لرفع كل الحلقات الجاهزة على DoodStream / Google Drive دفعة واحدة.

## بديل: نوتبوك Jupyter
`run_in_jupyter.ipynb` فيه نفس الخطوات خلايا جاهزة (تجهيز ← جوجل درايف ← تشغيل الواجهة ← متصفح Chrome).

## v4 — Long-Season Reliability & Consistency

- **Retry hardening**: كل نداء لـ DeepSeek يعيد المحاولة حتى 5 مرات بتراجع أسّي (exponential backoff) مع مهلة اتصال 180 ثانية — المواسم الطويلة تكمل حتى لو الشبكة اتقطعت لحظياً.
- **Tracker injection**: تحميل الماجنت/التورنت بيحقن تلقائياً قائمة trackers عامة (بتتحدث من ngosang/trackerslist وتتخزن في `/workspace/trackers.txt` لمدة 7 أيام، ومعها نسخة احتياطية مرفقة في `trackers.txt`) — التورنتات القديمة ضعيفة السيد بتلتقط أسرع بكتير.
- **Auto-learning glossary**: بعد كل حلقة، الأداة بتحفظ الأسماء والتقنيات زي ما اترجمت فعلاً في قاموس الموسم `/workspace/glossaries/<series>.json` — الحلقة 30 تطلع بنفس أسماء الحلقة 2. في قالب جاهز في `glossaries/glossary.example.json` تقدر تنسخه وتعدله لأي أنمي.

الترتيب ده تراكمي على v3: نفس الـ one-liner ونفس الواجهة، مفيش أي تغيير في طريقة التشغيل.

