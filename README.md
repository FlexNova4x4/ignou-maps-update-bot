IGNOU MAPS UPDATE BOT — TERMUX SETUP
====================================

यह Telegram bot IGNOU की public websites पर MAPS / M.A. Political Science से जुड़े assignments, notices और study-material links में बदलाव जाँचता है। नया लिंक/बदलाव मिलने पर Telegram message भेजता है। SQLite database duplicate alerts रोकता है।

1) Termux में packages install करें:
   pkg update && pkg upgrade
   pkg install python
   pip install requests beautifulsoup4

2) Telegram पर @BotFather खोलकर /newbot से bot बनाएँ। मिलने वाला token निजी रखें।

3) अपना Telegram chat ID निकालें:
   - अपने bot को खोलकर /start भेजें।
   - चाहें तो किसी reliable get-chat-id bot से अपना numeric chat ID देखें।
   - Group में updates चाहिए तो bot को group में जोड़ें और group chat ID दें।

4) इस folder में जाएँ और values set करें:
   export BOT_TOKEN='YAHAN_BOTFATHER_TOKEN'
   export CHAT_ID='YAHAN_NUMERIC_CHAT_ID'
   python ignou_maps_bot.py

5) Telegram bot chat में commands:
   /start  — help
   /check  — current matching links का snapshot
   /status — monitored URLs और interval

डिफ़ॉल्ट scan हर 30 मिनट में चलता है। जरूरत हो तो चलाने से पहले:
   export CHECK_INTERVAL=1800

डेटाबेस ignou_maps_bot.sqlite3 के रूप में बनता है; इसे हटाने पर bot अगली बार पुरानी links को baseline मानेगा और उन्हीं links पर alerts दोबारा नहीं भेजेगा, लेकिन baseline फिर से बनेगी।

TERMUX में चालू रखना:
   pkg install tmux
   tmux new -s mapsbot
   python ignou_maps_bot.py
   Ctrl+B, फिर D दबाकर tmux से बाहर निकलें। वापस आने के लिए:
   tmux attach -t mapsbot

ध्यान दें: tmux Termux process को Android द्वारा पूरी तरह kill होने से नहीं बचाता। ज्यादा भरोसेमंद 24/7 चलाने के लिए VPS/cloud hosting बेहतर है। मोबाइल पर Termux:Boot और battery-optimization settings की जरूरत पड़ सकती है।

यह bot केवल public pages पढ़ता है; login, CAPTCHA या private student records को bypass नहीं करता। IGNOU की original website से deadline और notice verify करें।
