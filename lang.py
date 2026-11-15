"""Resolved. — which language did the shop write in?

The reply goes back in the language the message was written in. The merchant's
registered language is only the fallback, when nothing in the message says otherwise.

  1. Script. Any Indian script (or Urdu's Arabic script) settles it: count characters per
     Unicode block, take the largest. Devanagari is Hindi unless Marathi words outnumber
     Hindi ones; Bengali script is Bengali unless it uses the letters only Assamese has.
  2. Romanised words. Latin-script Hindi, Tamil, Telugu and so on are matched against word
     lists — whole words, case-insensitive, at least TWO distinct markers before a language
     is called. Short words that collide across languages ("ne", "par", "en") are left out.
  3. Otherwise: the merchant's registered language.

What this is used for: the reply ALWAYS goes in the shop's chosen language. When the
message was written in a different one, the page offers "Translate to <that language>".
English is detected too (by its function words), so an English message to a Tamil shop
gets a Tamil reply plus "Translate to English". English only wins outright: on a tie with
an Indian language (Hinglish is full of English nouns), the Indian language is chosen.

The reply text for every language here is a fixed template (see REPLIES_EXTRA). Templates
for languages other than English, Tamil and Hindi are written by us and NOT yet checked by
native speakers; any template we do not have falls back to English, and the page says so.
"""
from __future__ import annotations

import re

NAMES = {"en": "English", "hi": "Hindi", "mr": "Marathi", "bn": "Bengali", "as": "Assamese",
         "pa": "Punjabi", "gu": "Gujarati", "or": "Odia", "ta": "Tamil", "te": "Telugu",
         "kn": "Kannada", "ml": "Malayalam", "ur": "Urdu"}

SCRIPTS = [  # (first, last, language)
    (0x0900, 0x097F, "hi"),   # Devanagari (Hindi or Marathi, decided below)
    (0x0980, 0x09FF, "bn"),   # Bengali-Assamese
    (0x0A00, 0x0A7F, "pa"),   # Gurmukhi
    (0x0A80, 0x0AFF, "gu"),   # Gujarati
    (0x0B00, 0x0B7F, "or"),   # Odia
    (0x0B80, 0x0BFF, "ta"),   # Tamil
    (0x0C00, 0x0C7F, "te"),   # Telugu
    (0x0C80, 0x0CFF, "kn"),   # Kannada
    (0x0D00, 0x0D7F, "ml"),   # Malayalam
    (0x0600, 0x06FF, "ur"),   # Arabic script, as Urdu writes it
]
ASSAMESE_ONLY = {"ৰ", "ৱ"}          # ৰ ৱ
MARATHI_DEVANAGARI = {"आहे", "माझ्या", "माझे", "माझा", "झाले", "केले", "आणि", "पण", "मला",
                      "आले", "नाहीत", "होते", "पाठवले"}
HINDI_DEVANAGARI = {"है", "हैं", "मेरे", "मेरा", "मुझे", "नहीं", "लेकिन", "आया", "गया", "भेजे",
                    "किया", "पैसे", "का", "की", "को"}

# Romanised markers. Order matters only to break exact ties (earlier wins).
ROMAN = {
    "hi": {"mere", "mera", "meri", "mujhe", "hai", "hain", "nahi", "nahin", "aaya", "aya",
           "bheje", "bheja", "bheji", "rupaye", "rupay", "iska", "uska", "lekin", "kyun",
           "kya", "gaya", "gaye", "hua", "hui", "abhi", "kiya", "diya", "mila", "mile",
           "aaj", "kal", "humne", "maine", "wapas", "kab", "kaise", "kripya", "bhai"},
    "ta": {"enga", "ennoda", "enoda", "kadai", "panam", "varala", "varalai", "varavillai",
           "illa", "illai", "anuppinaanga", "anupunanga", "anuppunaanga", "kaasu", "rubai",
           "irukku", "enna", "eppo", "netru", "nethu", "indru", "inniku", "aachu",
           "vandhuchu", "vanthuchu", "sollunga", "kitta", "vangala", "kudunga", "pannunga",
           "theriyala", "aagala", "romba", "konjam", "anuppunga", "vanga"},
    "te": {"naa", "nenu", "naaku", "raaledu", "raledu", "ledu", "dabbulu", "dabbu",
           "rupayalu", "vachindi", "pampadu", "pampindi", "pampaaru", "kani", "emi", "ela",
           "ivvaledu", "cheyandi", "kavali", "nijamga", "ninna", "eeroju", "vacchindi",
           "chesaru", "kottu", "andi"},
    "kn": {"nanna", "nange", "banthu", "bandilla", "barlilla", "duddu", "rupayi",
           "kalisidru", "aadre", "yaake", "beku", "aagilla", "maadi", "ide", "illva",
           "ivattu", "ninne", "kottilla", "bartilla", "kalsidru", "swalpa", "heli"},
    "ml": {"ente", "enikku", "vannilla", "kittiyilla", "ayachu", "pakshe", "undu", "alla",
           "cheythu", "aanu", "paisa_ml", "rupa", "ayachittund", "kitti", "innale", "innu",
           "onnum", "evide", "enthu", "venam", "parayu", "ningal"},
    "bn": {"amar", "ami", "taka", "pathiyeche", "pathalo", "pathiyechhe", "asheni",
           "kintu", "hoyni", "ache", "nei", "keno", "dokan", "amake", "pelam", "paini",
           "ekhono", "gotokal", "aajke", "korun", "dada", "bujhte"},
    "mr": {"majha", "majhya", "majhi", "maza", "mazya", "aahe", "ahe", "pathavle",
           "pathavla", "aale", "aala", "zhala", "jhala", "kela", "dukan", "mala", "pan",
           "kaal", "aaj_mr", "hota", "navhta", "milale", "nahit"},
    "gu": {"maaru", "maro", "mari", "chhe", "nathi", "aavyu", "avyu", "moklya", "moklyu",
           "rupiya", "kem", "aaje", "kaale", "mane", "tamne", "pan_gu", "malya", "thayu",
           "hatu", "dukan_gu", "kai"},
    "pa": {"mainu", "menu", "tusi", "tuhada", "sadda", "saada", "hoya", "ohne", "kithe",
           "kiven", "nahi_pa", "aaya_pa", "bhejeya", "bhejya", "paise_pa", "gal", "ji_pa",
           "karo", "sanu", "assi", "vich"},
    "ur": {"mujhay", "mere_ur", "nahin_ur", "aaya_ur", "shukriya", "janab", "lekin_ur",
           "hamari", "tashreef", "meharbani", "rupay_ur", "raqam", "wapsi"},
    "or": {"mora", "mo", "tanka", "asini", "aasini", "pathaichi", "kintu_or", "kahinki",
           "achhi", "nahin_or", "dokana", "aji", "kali", "hela", "heichi"},
    "as": {"mur", "moi", "pothaise", "ahi", "ahise", "nai", "kiyo", "dukan_as", "aji_as",
           "kali_as", "hol", "korise"},
    # function words only: Hinglish borrows nouns like "customer" and "payment", not these
    "en": {"the", "my", "is", "was", "but", "not", "and", "it", "from", "did", "didn",
           "has", "have", "never", "received", "reached", "showed", "yesterday", "today",
           "this", "that", "with", "please", "where", "when", "why", "what", "how",
           "money", "paid", "arrive", "arrived", "got"},
}
# Tokens with a _suffix above are placeholders kept so the lists read honestly: those words
# are shared with another language and must not be counted twice. They never match.

WORD = re.compile(r"[a-z]+")


def detect(message: str, registered: str) -> tuple[str, str]:
    """Returns (language, how): how is 'script', 'words' or 'registered'."""
    counts: dict[str, int] = {}
    for ch in message:
        cp = ord(ch)
        for lo, hi, lang in SCRIPTS:
            if lo <= cp <= hi:
                counts[lang] = counts.get(lang, 0) + 1
                break
    if counts:
        lang, n = max(counts.items(), key=lambda kv: kv[1])
        if n >= 2:
            if lang == "hi":
                words = set(re.findall(r"[ऀ-ॿ]+", message))
                if len(words & MARATHI_DEVANAGARI) > len(words & HINDI_DEVANAGARI):
                    lang = "mr"
            elif lang == "bn" and any(c in ASSAMESE_ONLY for c in message):
                lang = "as"
            return lang, "script"

    words = set(WORD.findall(message.lower()))
    scores = {lang: len(words & markers) for lang, markers in ROMAN.items()}
    indian = {k: v for k, v in scores.items() if k != "en"}
    best = max(indian.values())
    if best >= 2 and best >= scores["en"]:
        tied = [lang for lang, s in indian.items() if s == best]
        return (registered if registered in tied else tied[0]), "words"
    if scores["en"] >= 2:
        return "en", "words"
    return registered, "registered"


# ------------------------------------------------------------------ templates
# Core replies for the languages beyond English, Tamil and Hindi. Same placeholders as
# server.REPLIES. Written by us, NOT reviewed by native speakers — flagged in CLAUDE.md.
REPLIES_EXTRA = {
    "MARK_PAID": {
        "mr": "सापडले — ₹{amt} तुम्हाला मिळाले आहेत, साउंडबॉक्सने फक्त सांगितले नाही. {cycle} पावती: {utr}",
        "bn": "পেয়েছি — ₹{amt} আপনার কাছে পৌঁছেছে, সাউন্ডবক্স শুধু জানায়নি। {cycle} রসিদ: {utr}",
        "as": "পালোঁ — ₹{amt} আপোনাৰ ওচৰত পাইছে, চাউণ্ডবক্সে কেৱল জনোৱা নাই। {cycle} ৰচিদ: {utr}",
        "pa": "ਮਿਲ ਗਿਆ — ₹{amt} ਤੁਹਾਨੂੰ ਮਿਲ ਚੁੱਕਾ ਹੈ, ਸਾਊਂਡਬਾਕਸ ਨੇ ਦੱਸਿਆ ਨਹੀਂ। {cycle} ਰਸੀਦ: {utr}",
        "gu": "મળી ગયું — ₹{amt} તમને મળી ગયા છે, સાઉન્ડબોક્સે ફક્ત જણાવ્યું નહીં. {cycle} રસીદ: {utr}",
        "or": "ମିଳିଗଲା — ₹{amt} ଆପଣଙ୍କ ପାଖରେ ପହଞ୍ଚିଛି, ସାଉଣ୍ଡବକ୍ସ କେବଳ ଜଣାଇଲା ନାହିଁ। {cycle} ରସିଦ: {utr}",
        "te": "దొరికింది — ₹{amt} మీకు అందింది, సౌండ్‌బాక్స్ చెప్పలేదు అంతే. {cycle} రసీదు: {utr}",
        "kn": "ಸಿಕ್ಕಿತು — ₹{amt} ನಿಮಗೆ ತಲುಪಿದೆ, ಸೌಂಡ್‌ಬಾಕ್ಸ್ ಹೇಳಲಿಲ್ಲ ಅಷ್ಟೇ. {cycle} ರಶೀದಿ: {utr}",
        "ml": "കിട്ടി — ₹{amt} നിങ്ങൾക്ക് എത്തിയിട്ടുണ്ട്, സൗണ്ട്ബോക്സ് അറിയിച്ചില്ലെന്ന് മാത്രം. {cycle} രസീത്: {utr}",
        "ur": "مل گیا — ₹{amt} آپ کو مل چکے ہیں، ساؤنڈ باکس نے بس بتایا نہیں۔ {cycle} رسید: {utr}",
    },
    "REVERSE_TO_SOURCE": {
        "mr": "₹{amt} चे पेमेंट बँकेत अयशस्वी झाले. मी ते ग्राहकाला परत पाठवले आहे — २४ तासांत पोहोचेल. संदर्भ: {utr}",
        "bn": "₹{amt}-এর পেমেন্টটি ব্যাংকে ব্যর্থ হয়েছে। আমি এটি গ্রাহককে ফেরত পাঠিয়েছি — ২৪ ঘণ্টার মধ্যে পৌঁছাবে। রেফারেন্স: {utr}",
        "as": "₹{amt}ৰ পেমেণ্টটো বেংকত বিফল হ'ল। মই গ্ৰাহকলৈ ঘূৰাই পঠিয়াইছোঁ — ২৪ ঘণ্টাৰ ভিতৰত পাব। প্ৰসংগ: {utr}",
        "pa": "₹{amt} ਦਾ ਭੁਗਤਾਨ ਬੈਂਕ ਵਿੱਚ ਫੇਲ੍ਹ ਹੋ ਗਿਆ। ਮੈਂ ਇਹ ਗਾਹਕ ਨੂੰ ਵਾਪਸ ਭੇਜ ਦਿੱਤਾ ਹੈ — 24 ਘੰਟਿਆਂ ਵਿੱਚ ਪਹੁੰਚ ਜਾਵੇਗਾ। ਹਵਾਲਾ: {utr}",
        "gu": "₹{amt} નું પેમેન્ટ બેંકમાં નિષ્ફળ ગયું. મેં તે ગ્રાહકને પાછું મોકલ્યું છે — 24 કલાકમાં પહોંચશે. સંદર્ભ: {utr}",
        "or": "₹{amt} ପେମେଣ୍ଟ ବ୍ୟାଙ୍କରେ ବିଫଳ ହେଲା। ମୁଁ ଏହାକୁ ଗ୍ରାହକଙ୍କୁ ଫେରାଇ ଦେଇଛି — 24 ଘଣ୍ଟା ମଧ୍ୟରେ ପହଞ୍ଚିବ। ସନ୍ଦର୍ଭ: {utr}",
        "te": "₹{amt} చెల్లింపు బ్యాంకులో విఫలమైంది. దాన్ని కస్టమర్‌కు తిరిగి పంపాను — 24 గంటల్లో చేరుతుంది. రిఫరెన్స్: {utr}",
        "kn": "₹{amt} ಪಾವತಿ ಬ್ಯಾಂಕ್‌ನಲ್ಲಿ ವಿಫಲವಾಯಿತು. ಅದನ್ನು ಗ್ರಾಹಕರಿಗೆ ಹಿಂತಿರುಗಿಸಿದ್ದೇನೆ — 24 ಗಂಟೆಗಳಲ್ಲಿ ತಲುಪುತ್ತದೆ. ಉಲ್ಲೇಖ: {utr}",
        "ml": "₹{amt} പേയ്മെന്റ് ബാങ്കിൽ പരാജയപ്പെട്ടു. അത് ഉപഭോക്താവിന് തിരികെ അയച്ചു — 24 മണിക്കൂറിനുള്ളിൽ എത്തും. റഫറൻസ്: {utr}",
        "ur": "₹{amt} کی ادائیگی بینک میں ناکام ہو گئی۔ میں نے یہ گاہک کو واپس بھیج دی ہے — 24 گھنٹوں میں پہنچ جائے گی۔ حوالہ: {utr}",
    },
    "ESCALATE": {
        "mr": "मी तुमचे रेकॉर्ड तपासले आणि सापडलेले सर्व आमच्या टीमकडे पाठवले आहे. लवकरच उत्तर मिळेल.",
        "bn": "আমি আপনার রেকর্ড দেখেছি এবং যা পেয়েছি সব আমাদের টিমকে পাঠিয়েছি। শীঘ্রই উত্তর পাবেন।",
        "as": "মই আপোনাৰ ৰেকৰ্ড চালোঁ আৰু পোৱা সকলো আমাৰ দললৈ পঠিয়াইছোঁ। সোনকালে উত্তৰ পাব।",
        "pa": "ਮੈਂ ਤੁਹਾਡੇ ਰਿਕਾਰਡ ਵੇਖ ਲਏ ਹਨ ਅਤੇ ਸਭ ਕੁਝ ਸਾਡੀ ਟੀਮ ਨੂੰ ਭੇਜ ਦਿੱਤਾ ਹੈ। ਜਲਦੀ ਜਵਾਬ ਮਿਲੇਗਾ।",
        "gu": "મેં તમારા રેકોર્ડ તપાસ્યા અને જે મળ્યું તે બધું અમારી ટીમને મોકલ્યું છે. જલ્દી જવાબ મળશે.",
        "or": "ମୁଁ ଆପଣଙ୍କ ରେକର୍ଡ ଦେଖିଲି ଏବଂ ଯାହା ମିଳିଲା ସବୁ ଆମ ଟିମକୁ ପଠାଇଛି। ଶୀଘ୍ର ଉତ୍ତର ମିଳିବ।",
        "te": "మీ రికార్డులు చూశాను, దొరికినదంతా మా టీమ్‌కు పంపాను. త్వరలో సమాధానం వస్తుంది.",
        "kn": "ನಿಮ್ಮ ದಾಖಲೆಗಳನ್ನು ಪರಿಶೀಲಿಸಿ, ಸಿಕ್ಕಿದ್ದೆಲ್ಲವನ್ನೂ ನಮ್ಮ ತಂಡಕ್ಕೆ ಕಳುಹಿಸಿದ್ದೇನೆ. ಶೀಘ್ರದಲ್ಲೇ ಉತ್ತರ ಬರುತ್ತದೆ.",
        "ml": "നിങ്ങളുടെ രേഖകൾ പരിശോധിച്ചു, കണ്ടെത്തിയതെല്ലാം ഞങ്ങളുടെ ടീമിന് അയച്ചു. ഉടൻ മറുപടി ലഭിക്കും.",
        "ur": "میں نے آپ کا ریکارڈ دیکھ لیا ہے اور جو ملا سب ہماری ٹیم کو بھیج دیا ہے۔ جلد جواب ملے گا۔",
    },
    "BLOCK_AMBIGUOUS": {
        "mr": "त्या वेळी ₹{amt} ची एकापेक्षा जास्त पेमेंट्स दिसत आहेत, म्हणून चुकीच्याला हात लावणार नाही. ग्राहकाच्या अॅपमधील UTR पाठवा.",
        "bn": "ওই সময়ে ₹{amt}-এর একাধিক পেমেন্ট দেখছি, তাই ভুলটায় হাত দেব না। গ্রাহকের অ্যাপ থেকে UTR পাঠান।",
        "as": "সেই সময়ত ₹{amt}ৰ এটাতকৈ অধিক পেমেণ্ট দেখিছোঁ, গতিকে ভুলটো চুব নোখোজোঁ। গ্ৰাহকৰ এপৰ পৰা UTR পঠিয়াওক।",
        "pa": "ਉਸ ਸਮੇਂ ₹{amt} ਦੇ ਇੱਕ ਤੋਂ ਵੱਧ ਭੁਗਤਾਨ ਦਿਸ ਰਹੇ ਹਨ, ਇਸ ਲਈ ਗਲਤ ਵਾਲੇ ਨੂੰ ਹੱਥ ਨਹੀਂ ਲਾਵਾਂਗਾ। ਗਾਹਕ ਦੇ ਐਪ ਤੋਂ UTR ਭੇਜੋ।",
        "gu": "તે સમયે ₹{amt} ના એકથી વધુ પેમેન્ટ દેખાય છે, તેથી ખોટાને અડીશ નહીં. ગ્રાહકની એપમાંથી UTR મોકલો.",
        "or": "ସେହି ସମୟରେ ₹{amt} ର ଏକାଧିକ ପେମେଣ୍ଟ ଦେଖାଯାଉଛି, ତେଣୁ ଭୁଲଟିକୁ ଛୁଇଁବି ନାହିଁ। ଗ୍ରାହକଙ୍କ ଆପରୁ UTR ପଠାନ୍ତୁ।",
        "te": "ఆ సమయంలో ₹{amt} చెల్లింపులు ఒకటి కంటే ఎక్కువ ఉన్నాయి, కాబట్టి తప్పుదాన్ని ముట్టుకోను. కస్టమర్ యాప్ నుండి UTR పంపండి.",
        "kn": "ಆ ಸಮಯದಲ್ಲಿ ₹{amt} ಒಂದಕ್ಕಿಂತ ಹೆಚ್ಚು ಪಾವತಿಗಳಿವೆ, ಆದ್ದರಿಂದ ತಪ್ಪಾದದ್ದನ್ನು ಮುಟ್ಟುವುದಿಲ್ಲ. ಗ್ರಾಹಕರ ಆ್ಯಪ್‌ನಿಂದ UTR ಕಳುಹಿಸಿ.",
        "ml": "ആ സമയത്ത് ₹{amt} ന്റെ ഒന്നിലധികം പേയ്മെന്റുകൾ കാണുന്നു, അതിനാൽ തെറ്റായതിൽ തൊടില്ല. ഉപഭോക്താവിന്റെ ആപ്പിൽ നിന്ന് UTR അയയ്ക്കുക.",
        "ur": "اس وقت ₹{amt} کی ایک سے زیادہ ادائیگیاں نظر آ رہی ہیں، اس لیے غلط والی کو نہیں چھیڑوں گا۔ گاہک کی ایپ سے UTR بھیجیں۔",
    },
    "BLOCK_REDIRECT_PAYOUT": {
        "mr": "मी फक्त तुमच्या नोंदणीकृत पेआउट खात्यात पैसे पाठवू शकतो. खाते बदलण्यासाठी अॅपमध्ये बँक पडताळणी लागते. मी हे टीमसाठी नोंदवले आहे.",
        "bn": "আমি শুধু আপনার নিবন্ধিত পেআউট অ্যাকাউন্টে টাকা পাঠাতে পারি। অ্যাকাউন্ট বদলাতে অ্যাপে ব্যাংক যাচাই লাগে। টিমের জন্য চিহ্নিত করেছি।",
        "as": "মই কেৱল আপোনাৰ পঞ্জীয়নভুক্ত পেআউট একাউণ্টলৈ টকা পঠিয়াব পাৰোঁ। একাউণ্ট সলনি কৰিবলৈ এপত বেংক পৰীক্ষা লাগে। দলৰ বাবে চিহ্নিত কৰিলোঁ।",
        "pa": "ਮੈਂ ਸਿਰਫ਼ ਤੁਹਾਡੇ ਰਜਿਸਟਰਡ ਪੇਆਉਟ ਖਾਤੇ ਵਿੱਚ ਪੈਸੇ ਭੇਜ ਸਕਦਾ ਹਾਂ। ਖਾਤਾ ਬਦਲਣ ਲਈ ਐਪ ਵਿੱਚ ਬੈਂਕ ਤਸਦੀਕ ਚਾਹੀਦੀ ਹੈ। ਟੀਮ ਲਈ ਨਿਸ਼ਾਨ ਲਾ ਦਿੱਤਾ ਹੈ।",
        "gu": "હું ફક્ત તમારા નોંધાયેલ પેઆઉટ ખાતામાં પૈસા મોકલી શકું. ખાતું બદલવા એપમાં બેંક ચકાસણી જોઈએ. ટીમ માટે નોંધ્યું છે.",
        "or": "ମୁଁ କେବଳ ଆପଣଙ୍କ ପଞ୍ଜୀକୃତ ପେଆଉଟ ଆକାଉଣ୍ଟକୁ ଟଙ୍କା ପଠାଇପାରିବି। ଆକାଉଣ୍ଟ ବଦଳାଇବାକୁ ଆପରେ ବ୍ୟାଙ୍କ ଯାଞ୍ଚ ଦରକାର। ଟିମ ପାଇଁ ଚିହ୍ନିତ କରିଛି।",
        "te": "మీ రిజిస్టర్డ్ పేఅవుట్ ఖాతాకే డబ్బు పంపగలను. ఖాతా మార్చాలంటే యాప్‌లో బ్యాంక్ ధృవీకరణ కావాలి. టీమ్ కోసం గుర్తించాను.",
        "kn": "ನಿಮ್ಮ ನೋಂದಾಯಿತ ಪೇಔಟ್ ಖಾತೆಗೆ ಮಾತ್ರ ಹಣ ಕಳುಹಿಸಬಹುದು. ಖಾತೆ ಬದಲಿಸಲು ಆ್ಯಪ್‌ನಲ್ಲಿ ಬ್ಯಾಂಕ್ ಪರಿಶೀಲನೆ ಬೇಕು. ತಂಡಕ್ಕಾಗಿ ಗುರುತಿಸಿದ್ದೇನೆ.",
        "ml": "നിങ്ങളുടെ രജിസ്റ്റർ ചെയ്ത പേഔട്ട് അക്കൗണ്ടിലേക്ക് മാത്രമേ പണം അയയ്ക്കാനാകൂ. അക്കൗണ്ട് മാറ്റാൻ ആപ്പിൽ ബാങ്ക് പരിശോധന വേണം. ടീമിനായി അടയാളപ്പെടുത്തി.",
        "ur": "میں صرف آپ کے رجسٹرڈ پے آؤٹ اکاؤنٹ میں پیسے بھیج سکتا ہوں۔ اکاؤنٹ بدلنے کے لیے ایپ میں بینک تصدیق چاہیے۔ ٹیم کے لیے نشان لگا دیا ہے۔",
    },
    "NONE": {
        "mr": "{when}तुमच्या दुकानात ₹{amt} चे पेमेंट सापडले नाही. ग्राहकाच्या अॅपमधील UTR पाठवा, मी पुन्हा पाहतो.",
        "bn": "{when}আপনার দোকানে ₹{amt}-এর কোনো পেমেন্ট পাইনি। গ্রাহকের অ্যাপ থেকে UTR পাঠান, আবার দেখব।",
        "as": "{when}আপোনাৰ দোকানত ₹{amt}ৰ কোনো পেমেণ্ট পোৱা নাই। গ্ৰাহকৰ এপৰ পৰা UTR পঠিয়াওক, পুনৰ চাম।",
        "pa": "{when}ਤੁਹਾਡੀ ਦੁਕਾਨ 'ਤੇ ₹{amt} ਦਾ ਕੋਈ ਭੁਗਤਾਨ ਨਹੀਂ ਮਿਲਿਆ। ਗਾਹਕ ਦੇ ਐਪ ਤੋਂ UTR ਭੇਜੋ, ਮੈਂ ਫਿਰ ਵੇਖਾਂਗਾ।",
        "gu": "{when}તમારી દુકાને ₹{amt} નું કોઈ પેમેન્ટ મળ્યું નહીં. ગ્રાહકની એપમાંથી UTR મોકલો, હું ફરી જોઈશ.",
        "or": "{when}ଆପଣଙ୍କ ଦୋକାନରେ ₹{amt} ର କୌଣସି ପେମେଣ୍ଟ ମିଳିଲା ନାହିଁ। ଗ୍ରାହକଙ୍କ ଆପରୁ UTR ପଠାନ୍ତୁ, ପୁଣି ଦେଖିବି।",
        "te": "{when}మీ షాపుకు ₹{amt} చెల్లింపు కనిపించలేదు. కస్టమర్ యాప్ నుండి UTR పంపండి, మళ్లీ చూస్తాను.",
        "kn": "{when}ನಿಮ್ಮ ಅಂಗಡಿಗೆ ₹{amt} ಪಾವತಿ ಸಿಗಲಿಲ್ಲ. ಗ್ರಾಹಕರ ಆ್ಯಪ್‌ನಿಂದ UTR ಕಳುಹಿಸಿ, ಮತ್ತೆ ನೋಡುತ್ತೇನೆ.",
        "ml": "{when}നിങ്ങളുടെ കടയിലേക്ക് ₹{amt} ന്റെ പേയ്മെന്റ് കണ്ടെത്തിയില്ല. ഉപഭോക്താവിന്റെ ആപ്പിൽ നിന്ന് UTR അയയ്ക്കൂ, വീണ്ടും നോക്കാം.",
        "ur": "{when}آپ کی دکان پر ₹{amt} کی کوئی ادائیگی نہیں ملی۔ گاہک کی ایپ سے UTR بھیجیں، میں دوبارہ دیکھتا ہوں۔",
    },
    "ASK_AMOUNT": {
        "mr": "मी पाहतो. पेमेंट किती होते आणि साधारण केव्हा? ग्राहकाच्या अॅपमधील UTR असेल तर आणखी चांगले.",
        "bn": "দেখছি। পেমেন্ট কত টাকার ছিল আর মোটামুটি কখন? গ্রাহকের অ্যাপের UTR থাকলে আরও ভালো।",
        "as": "চাওঁ। পেমেণ্ট কিমান আছিল আৰু প্ৰায় কেতিয়া? গ্ৰাহকৰ এপৰ UTR থাকিলে আৰু ভাল।",
        "pa": "ਮੈਂ ਵੇਖਦਾ ਹਾਂ। ਭੁਗਤਾਨ ਕਿੰਨੇ ਦਾ ਸੀ ਅਤੇ ਲਗਭਗ ਕਦੋਂ? ਗਾਹਕ ਦੇ ਐਪ ਦਾ UTR ਹੋਵੇ ਤਾਂ ਹੋਰ ਵਧੀਆ।",
        "gu": "હું જોઉં છું. પેમેન્ટ કેટલાનું હતું અને લગભગ ક્યારે? ગ્રાહકની એપનો UTR હોય તો વધુ સારું.",
        "or": "ମୁଁ ଦେଖୁଛି। ପେମେଣ୍ଟ କେତେ ଥିଲା ଏବଂ ପ୍ରାୟ କେବେ? ଗ୍ରାହକଙ୍କ ଆପର UTR ଥିଲେ ଆହୁରି ଭଲ।",
        "te": "చూస్తాను. చెల్లింపు ఎంత, సుమారు ఎప్పుడు? కస్టమర్ యాప్‌లోని UTR ఉంటే ఇంకా మంచిది.",
        "kn": "ನೋಡುತ್ತೇನೆ. ಪಾವತಿ ಎಷ್ಟು, ಸುಮಾರು ಯಾವಾಗ? ಗ್ರಾಹಕರ ಆ್ಯಪ್‌ನ UTR ಇದ್ದರೆ ಇನ್ನೂ ಒಳ್ಳೆಯದು.",
        "ml": "നോക്കാം. പേയ്മെന്റ് എത്രയായിരുന്നു, ഏകദേശം എപ്പോൾ? ഉപഭോക്താവിന്റെ ആപ്പിലെ UTR ഉണ്ടെങ്കിൽ കൂടുതൽ നല്ലത്.",
        "ur": "میں دیکھتا ہوں۔ ادائیگی کتنی تھی اور تقریباً کب؟ گاہک کی ایپ کا UTR ہو تو اور بہتر۔",
    },
    "ALREADY_SETTLED": {
        "mr": "ते ₹{amt} आधीच तुमच्या खात्यात जमा झाले आहेत. आणखी काही करायचे नाही. पावती: {utr}",
        "bn": "ওই ₹{amt} আগেই আপনার অ্যাকাউন্টে জমা হয়েছে। আর কিছু করার নেই। রসিদ: {utr}",
        "as": "সেই ₹{amt} আগতেই আপোনাৰ একাউণ্টত জমা হৈছে। আৰু একো কৰিব নালাগে। ৰচিদ: {utr}",
        "pa": "ਉਹ ₹{amt} ਪਹਿਲਾਂ ਹੀ ਤੁਹਾਡੇ ਖਾਤੇ ਵਿੱਚ ਜਮ੍ਹਾ ਹੋ ਚੁੱਕੇ ਹਨ। ਹੋਰ ਕੁਝ ਕਰਨ ਦੀ ਲੋੜ ਨਹੀਂ। ਰਸੀਦ: {utr}",
        "gu": "તે ₹{amt} પહેલેથી તમારા ખાતામાં જમા થઈ ગયા છે. બીજું કંઈ કરવાનું નથી. રસીદ: {utr}",
        "or": "ସେହି ₹{amt} ପୂର୍ବରୁ ଆପଣଙ୍କ ଆକାଉଣ୍ଟରେ ଜମା ହୋଇସାରିଛି। ଆଉ କିଛି କରିବାକୁ ନାହିଁ। ରସିଦ: {utr}",
        "te": "ఆ ₹{amt} ఇప్పటికే మీ ఖాతాలో జమ అయింది. ఇంకేమీ చేయనక్కర్లేదు. రసీదు: {utr}",
        "kn": "ಆ ₹{amt} ಈಗಾಗಲೇ ನಿಮ್ಮ ಖಾತೆಗೆ ಜಮೆಯಾಗಿದೆ. ಇನ್ನೇನೂ ಮಾಡಬೇಕಿಲ್ಲ. ರಶೀದಿ: {utr}",
        "ml": "ആ ₹{amt} ഇതിനകം നിങ്ങളുടെ അക്കൗണ്ടിൽ വരവുവച്ചിട്ടുണ്ട്. ഇനി ഒന്നും ചെയ്യേണ്ടതില്ല. രസീത്: {utr}",
        "ur": "وہ ₹{amt} پہلے ہی آپ کے اکاؤنٹ میں جمع ہو چکے ہیں۔ مزید کچھ کرنے کی ضرورت نہیں۔ رسید: {utr}",
    },
    "RESOLVED_ON_UTR": {
        "mr": "मिळाले — त्या UTR मुळे फरक स्पष्ट झाला. ", "bn": "পেয়েছি — ওই UTR দিয়ে আলাদা করা গেল। ",
        "as": "পালোঁ — সেই UTRৰ পৰা পাৰ্থক্য স্পষ্ট হ'ল। ", "pa": "ਮਿਲ ਗਿਆ — ਉਸ UTR ਨਾਲ ਫ਼ਰਕ ਸਾਫ਼ ਹੋ ਗਿਆ। ",
        "gu": "મળ્યું — તે UTR થી ફરક સ્પષ્ટ થયો. ", "or": "ମିଳିଲା — ସେହି UTR ରୁ ପାର୍ଥକ୍ୟ ସ୍ପଷ୍ଟ ହେଲା। ",
        "te": "దొరికింది — ఆ UTR తో తేడా తెలిసింది. ", "kn": "ಸಿಕ್ಕಿತು — ಆ UTR ನಿಂದ ವ್ಯತ್ಯಾಸ ಸ್ಪಷ್ಟವಾಯಿತು. ",
        "ml": "കിട്ടി — ആ UTR കൊണ്ട് വ്യത്യാസം വ്യക്തമായി. ", "ur": "مل گیا — اس UTR سے فرق واضح ہو گیا۔ ",
    },
}
HELP_EXTRA = {
    "mr": "मी विवादित पेमेंट्स सोडवतो — ग्राहकाचे पैसे कापले गेले पण तुमच्यापर्यंत आले नाहीत. रक्कम आणि साधारण वेळ सांगा, मी रेकॉर्ड तपासतो.",
    "bn": "আমি বিতর্কিত পেমেন্টের সমাধান করি — গ্রাহকের টাকা কেটেছে কিন্তু আপনার কাছে আসেনি। টাকার পরিমাণ আর মোটামুটি সময় বলুন, রেকর্ড দেখব।",
    "as": "মই বিতৰ্কিত পেমেণ্ট সমাধান কৰোঁ — গ্ৰাহকৰ টকা কাটিছে কিন্তু আপোনাৰ ওচৰলৈ অহা নাই। পৰিমাণ আৰু প্ৰায় সময় কওক।",
    "pa": "ਮੈਂ ਭੁਗਤਾਨ ਦੇ ਝਗੜੇ ਸੁਲਝਾਉਂਦਾ ਹਾਂ — ਜਦੋਂ ਗਾਹਕ ਦੇ ਪੈਸੇ ਕੱਟੇ ਗਏ ਪਰ ਤੁਹਾਡੇ ਤੱਕ ਨਹੀਂ ਪਹੁੰਚੇ। ਰਕਮ ਅਤੇ ਲਗਭਗ ਸਮਾਂ ਦੱਸੋ।",
    "gu": "હું વિવાદિત પેમેન્ટ ઉકેલું છું — ગ્રાહકના પૈસા કપાયા પણ તમને મળ્યા નહીં. રકમ અને લગભગ સમય જણાવો, હું રેકોર્ડ તપાસીશ.",
    "or": "ମୁଁ ବିବାଦୀୟ ପେମେଣ୍ଟ ସମାଧାନ କରେ — ଗ୍ରାହକଙ୍କ ଟଙ୍କା କଟିଛି କିନ୍ତୁ ଆପଣଙ୍କ ପାଖରେ ପହଞ୍ଚିନାହିଁ। ରାଶି ଏବଂ ପ୍ରାୟ ସମୟ କୁହନ୍ତୁ।",
    "te": "వివాదాస్పద చెల్లింపులను నేను పరిష్కరిస్తాను — కస్టమర్ డబ్బు కట్ అయి మీకు చేరనప్పుడు. మొత్తం, సుమారు సమయం చెప్పండి, రికార్డు చూస్తాను.",
    "kn": "ವಿವಾದಿತ ಪಾವತಿಗಳನ್ನು ನಾನು ಬಗೆಹರಿಸುತ್ತೇನೆ — ಗ್ರಾಹಕರ ಹಣ ಕಡಿತವಾಗಿ ನಿಮಗೆ ತಲುಪದಿದ್ದಾಗ. ಮೊತ್ತ ಮತ್ತು ಸುಮಾರು ಸಮಯ ತಿಳಿಸಿ.",
    "ml": "തർക്കമുള്ള പേയ്മെന്റുകൾ ഞാൻ പരിഹരിക്കും — ഉപഭോക്താവിന്റെ പണം പോയിട്ടും നിങ്ങൾക്ക് എത്താത്തപ്പോൾ. തുകയും ഏകദേശ സമയവും പറയൂ.",
    "ur": "میں ادائیگی کے تنازعات حل کرتا ہوں — جب گاہک کے پیسے کٹ گئے مگر آپ تک نہیں پہنچے۔ رقم اور تقریباً وقت بتائیں، میں ریکارڈ دیکھتا ہوں۔",
}
CYCLE_EXTRA = {
    "mr": ("आजच्या सेटलमेंटमध्ये जाईल (कट-ऑफ {cut}).", "आजचा {cut} कट-ऑफ गेला, म्हणून उद्याच्या सेटलमेंटमध्ये जाईल."),
    "bn": ("আজকের সেটলমেন্টে যাবে (কাট-অফ {cut})।", "আজকের {cut} কাট-অফ পেরিয়ে গেছে, তাই কালকের সেটলমেন্টে যাবে।"),
    "as": ("আজিৰ চেটলমেণ্টত যাব (কাট-অফ {cut})।", "আজিৰ {cut} কাট-অফ পাৰ হৈছে, গতিকে কাইলৈৰ চেটলমেণ্টত যাব।"),
    "pa": ("ਇਹ ਅੱਜ ਦੀ ਸੈਟਲਮੈਂਟ ਵਿੱਚ ਜਾਵੇਗਾ (ਕੱਟ-ਆਫ਼ {cut})।", "ਅੱਜ ਦਾ {cut} ਕੱਟ-ਆਫ਼ ਲੰਘ ਗਿਆ, ਇਸ ਲਈ ਕੱਲ੍ਹ ਦੀ ਸੈਟਲਮੈਂਟ ਵਿੱਚ ਜਾਵੇਗਾ।"),
    "gu": ("આજના સેટલમેન્ટમાં જશે (કટ-ઓફ {cut}).", "આજનો {cut} કટ-ઓફ વીતી ગયો, તેથી આવતીકાલના સેટલમેન્ટમાં જશે."),
    "or": ("ଆଜିର ସେଟଲମେଣ୍ଟରେ ଯିବ (କଟ-ଅଫ {cut})।", "ଆଜିର {cut} କଟ-ଅଫ ପାର ହୋଇଗଲା, ତେଣୁ କାଲିର ସେଟଲମେଣ୍ଟରେ ଯିବ।"),
    "te": ("ఈరోజు సెటిల్‌మెంట్‌లో చేరుతుంది (కట్-ఆఫ్ {cut}).", "ఈరోజు {cut} కట్-ఆఫ్ దాటింది, కాబట్టి రేపటి సెటిల్‌మెంట్‌లో చేరుతుంది."),
    "kn": ("ಇಂದಿನ ಸೆಟಲ್‌ಮೆಂಟ್‌ನಲ್ಲಿ ಸೇರುತ್ತದೆ (ಕಟ್-ಆಫ್ {cut}).", "ಇಂದಿನ {cut} ಕಟ್-ಆಫ್ ಮೀರಿದೆ, ಆದ್ದರಿಂದ ನಾಳೆಯ ಸೆಟಲ್‌ಮೆಂಟ್‌ನಲ್ಲಿ ಸೇರುತ್ತದೆ."),
    "ml": ("ഇന്നത്തെ സെറ്റിൽമെന്റിൽ ഉൾപ്പെടും (കട്ട്-ഓഫ് {cut}).", "ഇന്നത്തെ {cut} കട്ട്-ഓഫ് കഴിഞ്ഞു, അതിനാൽ നാളത്തെ സെറ്റിൽമെന്റിൽ ഉൾപ്പെടും."),
    "ur": ("یہ آج کی سیٹلمنٹ میں جائے گا (کٹ آف {cut})۔", "آج کا {cut} کٹ آف گزر گیا، اس لیے کل کی سیٹلمنٹ میں جائے گا۔"),
}
WHEN_EXTRA = {"mr": "त्या वेळी ", "bn": "ওই সময়ে ", "as": "সেই সময়ত ", "pa": "ਉਸ ਸਮੇਂ ",
              "gu": "તે સમયે ", "or": "ସେହି ସମୟରେ ", "te": "ఆ సమయంలో ", "kn": "ಆ ಸಮಯದಲ್ಲಿ ",
              "ml": "ആ സമയത്ത് ", "ur": "اس وقت "}
