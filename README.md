# Runpod Anime Subber (Multi-Quality Links + Local Storage + MEGA / DoodStream)

أداة بواجهة ويب (**Gradio**) جاهزة للرفع على **GitHub** والتشغيل من **JupyterLab** على **Runpod**:
- إدخال روابط أو ملفات تورنت منفصلة لكل جودة (**1080p** / **720p** / **480p**).
- سحب الترجمة المرفقة من الـ MKV (أو تفريغها بـ **Faster-Whisper**) وترجمتها **مرة واحدة** للعربية عبر Gemini API أو موديل محلي.
- تطبيق ستايل الـ 7 فونتات العربية من الملف المستقل `style_template.ass` (قابل للتعديل بـ Aegisub) مع فحص إجباري لوجود الـ 7 فونتات داخل `fonts/` قبل الدمج.
- دمج الترجمة وضغط كل جودة بدقتها الأصلية عبر **FFmpeg + NVIDIA NVENC**.
- حفظ النسخ النهائية في `/workspace/outputs/<اسم_الحلقة>` للرفع المحلي على **YourUpload** و **Videa.hu**، مع رفع تلقائي واستخراج روابط على **MEGA** و **DoodStream** و **Google Drive**.

## خطوات الرفع والتشغيل
1. فك ضغط الملف وارفع محتويات المجلد (بما فيها `fonts/` و `style_template.ass`) إلى مستودع على حسابك في GitHub.
2. على **Runpod** افتح **JupyterLab** وارفع `run_in_jupyter.ipynb`.
3. عدّل `GITHUB_REPO` في الخلية 1، وبيانات **MEGA** في الخلية 2، وشغّل الخلية 3 لفتح الواجهة.
