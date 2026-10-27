# Telugu mode: five-task test and 60-second demo

For Gnani AI's Great Indian AI Internship Challenge (Main Character Award, regional language).
Rule: **synthetic data only**. Every screen below uses made-up names, numbers and files. Don't show real email, contacts or accounts.

## Setup (once, ~10 min, on 5 Nov)

1. Deploy the Worker with `GNANI_API_KEY` set, and run `python scripts/gnani_smoke.py --steps realtime`.
2. In the tray, set **Language → తెలుగు (Gnani)**.
3. Prepare the synthetic screens in `demo/` (a fake shop sheet, a fake form and a buggy script; names like "Sri Lakshmi Kirana", "Ravi Kumar").
4. Open `latency.log` after each task to read the timings.

## Five-task test (5–6 Nov)

Say each prompt as written. Telugu-English mixing is intentional, to test code-switching.

| # | Screen (synthetic) | Say this | Pass if |
|---|---|---|---|
| 1 | Excel: Sri Lakshmi Kirana monthly expenses, 6 rows of ₹ amounts | "ఈ sheet లో total ఎలా వేయాలి?" | Points at AutoSum or the empty total cell; reads the total aloud in Telugu rupees (e.g. "పన్నెండు వేల నాలుగు వందల రూపాయలు") |
| 2 | Windows Settings → Network | "Wi-Fi ఎక్కడ off చేయాలి?" | Pointer lands on the Wi-Fi toggle; reply is 1–2 short Telugu sentences |
| 3 | Local HTML form: fake college application | "ఈ form లో date of birth ఎక్కడ fill చేయాలి?" | Points at the DOB field; says the format (DD/MM/YYYY) clearly |
| 4 | Fake calendar: "Project review, 3:30 PM, 7 Nov" | "నా meeting ఎప్పుడు?" | Says "నవంబర్ ఏడో తేదీ మధ్యాహ్నం మూడున్నరకు" (or similar); no digits read in English |
| 5 | VS Code: `demo/bill.py` with a `TypeError` on line 12 | "ఈ error ఏంటి, ఎలా fix చేయాలి?" | Points at line 12; explains in Telugu, keeps "TypeError" / "int" in English |

Record for each task: transcript correct? (Y/N), Telugu reply? (Y/N), pointer on target? (Y/N), numbers spoken right? (Y/N), **speech end → first audio** (s) from `latency.log`.
Target: 5/5 pointer hits, first audio under 2.0 s. Retry a failed task once and note why it failed.

## 60-second demo video (7 Nov)

Record with OBS at 1080p, plus mic audio. Add English subtitles for the judges. HeyBuddy's own voice stays in Telugu.

| Time | On screen | Audio |
|---|---|---|
| 0–5 s | Title card: "HeyBuddy — మీ screen పక్కన ఒక తెలుగు buddy" | You: "ఇది HeyBuddy. Telugu లో మాట్లాడే screen assistant." |
| 5–10 s | Tray menu → Language → తెలుగు (Gnani) | You: "Gnani Prisma తో వింటుంది, Timbre తో మాట్లాడుతుంది." |
| 10–25 s | Task 1 (Excel). Hold Ctrl+Alt and ask | Buddy points at AutoSum and says the total in Telugu rupees |
| 25–38 s | Task 5 (VS Code error) | Buddy points at line 12 and explains, mixing English terms naturally |
| 38–50 s | Task 4 (calendar), with `latency.log` beside it | Buddy says the time; caption shows "first audio: X.X s" (your real number) |
| 50–60 s | End card: GitHub link, #GnaniAI #GreatIndianAIInternshipChallenge | You: "మన భాషలో, మన screen మీద. HeyBuddy." |

Tips: do 3 takes and keep the best. Cut dead air but never speed up the buddy's replies, since the judges will be looking at latency. Use only synthetic screens.

## Post (8 Nov), draft

> Built HeyBuddy's Telugu mode for #GreatIndianAIInternshipChallenge 🎙️
> Hold a key, ask in Telugu (or Tenglish), and HeyBuddy looks at your screen, points to the right button and answers in natural Telugu speech.
> • Speech-to-text: Gnani Prisma v2.5 (te-IN)
> • Voice: Gnani Timbre v2.5
> • Handles numbers, ₹, times and English tech words inside Telugu sentences
> Demo 👇 Code: github.com/Hem456anth/HeyBuddy
> #GnaniAI #GreatIndianAIInternshipChallenge
