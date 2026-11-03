# Summarizer comparison

- **A**: qwen3.5:4b / v1
- **B**: gemma4:e4b / v1
- Videos in the golden set: 5
- Missing A: 3
- Missing B: 1

## Summary

| Video | A claims | B claims | A quotes | B quotes | A unknown claims | B unknown claims | A tokens | B tokens | A duration | B duration |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Everything You Need to Know About Peptides | - | 16 | - | 12 | - | 0/16 | - | 14008 | - | 950946 ms |
| Most Men Will Get Prostate Cancer As They Age \(Here’s What To Do\) | 12 | 12 | 8 | 10 | 1/12 | 0/12 | 12228 | 13046 | 903688 ms | 877788 ms |
| The Last EASY Trade Before It Explodes | 23 | 16 | 10 | 15 | 16/23 | 16/16 | 12349 | 12766 | 937565 ms | 829751 ms |
| United Health Group Stock Drops 50% and Moody’s Lowers US Credit Rating | - | 37 | - | 21 | - | 36/37 | - | 24118 | - | 1589944 ms |
| Google Stock Will Keep Going Higher, Here’s Why | - | - | - | - | - | - | - | - | - | - |
| **Total** | 35 | 81 | 18 | 58 | 17/35 | 52/81 | 24577 | 63938 | 1841253 ms | 4248429 ms |

### Usage totals

|  | A | B |
| --- | --- | --- |
| Total cache read tokens | 0 | 0 |
| Total cache write tokens | 0 | 0 |
| Total model calls | 6 | 14 |

## Everything You Need to Know About Peptides (qovnfjVd8cs)

[Watch on YouTube](https://www.youtube.com/watch?v=qovnfjVd8cs)

|  | A | B |
| --- | --- | --- |
| Transcript source | - | youtube\_auto |
| Chunk strategy | - | time:900:60 |
| Created (UTC) | - | 2026-11-03 01:42:45 |
| Tokens (in / out) | - | 11526 / 2482 |
| Cache read tokens | - | 0 |
| Cache write tokens | - | 0 |
| Model calls | - | 3 |
| Duration (ms) | - | 950946 |
| Cost (USD) | - | not recorded |
| Topics / claims / quotes | - | 5 / 16 / 12 |
| Unattributed claims | - | 0 of 16 |
| Claim confidence (high / medium / low / not given) | - | 14 / 2 / 0 / 0 |
| Speakers coerced to unknown | - | 0 |

### Run A

No analysis for qwen3.5:4b / v1

### Run B

Roster

- Dr. Eric Berg, DC (host)

**TL;DR B:** Peptides are short chains made from amino acids, acting as blueprints that signal the body to perform specific functions, which contrasts with long protein chains found in food like steak. Different peptides operate in various ways, such as being a "dimmer switch" or a "new lamp," with examples like Sermorelin activating growth hormone. Specific peptides like BPC-157 are noted for tissue repair, and GHK copper peptide is mentioned for collagen synthesis, though research on peptides should be viewed alongside evidence-based lifestyle advice. Significant concerns exist regarding peptide safety, with reports showing that up to 30% may contain the wrong amino acid sequence and that many fail bacterial endotoxin limits. Ultimately, the discussion advises that healthy goals can be supported naturally through exercise, such as sprinting or intermittent fasting, alongside understanding the risks of unverified sourcing.

Topics

- [0:00](https://www.youtube.com/watch?v=qovnfjVd8cs&t=0s) Understanding Peptides
- [2:18](https://www.youtube.com/watch?v=qovnfjVd8cs&t=138s) Types and Mechanisms of Peptides
- [4:00](https://www.youtube.com/watch?v=qovnfjVd8cs&t=240s) Specific Peptide Applications and Evidence
- [7:20](https://www.youtube.com/watch?v=qovnfjVd8cs&t=440s) Safety and Sourcing Concerns
- [10:14](https://www.youtube.com/watch?v=qovnfjVd8cs&t=614s) Natural and Alternative Methods for Health

### Claims

#### 0:00-5:00

- **B** · [0:39](https://www.youtube.com/watch?v=qovnfjVd8cs&t=39s) · Dr. Eric Berg, DC · high · A peptide is made from amino acids. A peptide is a very short chain, okay, versus like if you were to eat a steak, piece of protein, that's a very long chain.
- **B** · [0:52](https://www.youtube.com/watch?v=qovnfjVd8cs&t=52s) · Dr. Eric Berg, DC · high · They're short chains that are basically messages that will give a signal to tell the body to do a certain thing. So, look at them like a blueprint.
- **B** · [1:15](https://www.youtube.com/watch?v=qovnfjVd8cs&t=75s) · Dr. Eric Berg, DC · high · A peptide can actually turn up the dial with that growth hormone. It can enhance the hormone that you already have that might be lowered because of age.
- **B** · [1:47](https://www.youtube.com/watch?v=qovnfjVd8cs&t=107s) · Dr. Eric Berg, DC · high · Steroids are made from cholesterol. Then you have collagen powder, which is peptides, but I want you to look at that as more like raw bricks to build certain things.
- **B** · [2:01](https://www.youtube.com/watch?v=qovnfjVd8cs&t=121s) · Dr. Eric Berg, DC · high · The single question that will really help you understand this whole topic, are you forcing the body to do something it was never meant to do, or are you just helping the body do something that it already is supposed to do, but it's not doing it?
- **B** · [2:25](https://www.youtube.com/watch?v=qovnfjVd8cs&t=145s) · Dr. Eric Berg, DC · high · Sermorelin actually activates growth hormone.
- **B** · [3:15](https://www.youtube.com/watch?v=qovnfjVd8cs&t=195s) · Dr. Eric Berg, DC · high · GLP-1s are engineered to be active in the bloodstream for seven straight days.
- **B** · [3:46](https://www.youtube.com/watch?v=qovnfjVd8cs&t=226s) · Dr. Eric Berg, DC · high · A good portion of that weight loss \[from GLP-1s\] is muscle. Gain the weight back, but not the muscle. They gain the fat back.
- **B** · [4:22](https://www.youtube.com/watch?v=qovnfjVd8cs&t=262s) · Dr. Eric Berg, DC · medium · The research on rats is amazing. I mean, there's like 50% more blood vessels in the damaged area. Healing is accelerated across muscle, tendon, ligament areas.

#### 5:00-10:00

- **B** · [5:25](https://www.youtube.com/watch?v=qovnfjVd8cs&t=325s) · Dr. Eric Berg, DC · medium · In another study, they had 16 people and they found 14 out of the 16 people had significant reduction in pain within 6 to 12 months.
- **B** · [6:40](https://www.youtube.com/watch?v=qovnfjVd8cs&t=400s) · Dr. Eric Berg, DC · high · BPC-157 does not trigger cancer. But, if you have an active cancer, probably not a good idea to take it because it's going to increase more blood flow to a tumor potentially.
- **B** · [7:40](https://www.youtube.com/watch?v=qovnfjVd8cs&t=460s) · Dr. Eric Berg, DC · high · 30% contain the wrong amino acid sequence. So, you would be injecting a completely different chemical than what was on the label.
- **B** · [7:48](https://www.youtube.com/watch?v=qovnfjVd8cs&t=468s) · Dr. Eric Berg, DC · high · 65% failed bacterial endotoxin limits. These are bacterial toxins that survived the sterilization process and can trigger immune reactions.
- **B** · [8:16](https://www.youtube.com/watch?v=qovnfjVd8cs&t=496s) · Dr. Eric Berg, DC · high · HPLC does not check for heavy metals, does not check for bacterial toxins, it does not verify if the product is sterile or standardized.

#### 10:00-15:00

- **B** · [10:36](https://www.youtube.com/watch?v=qovnfjVd8cs&t=636s) · Dr. Eric Berg, DC · high · The biggest thing is the type of exercise called sprinting or high-intensity interval training.
- **B** · [11:04](https://www.youtube.com/watch?v=qovnfjVd8cs&t=664s) · Dr. Eric Berg, DC · high · Fasting is another trigger for growth hormone, okay. If you do intermittent fasting and periodic prolonged fasting, you can spike your growth hormone tremendously.

### Quotes

#### 0:00-5:00

- **B** · [0:00](https://www.youtube.com/watch?v=qovnfjVd8cs&t=0s) · Dr. Eric Berg, DC · I'm going to tell you everything you ever need to know about peptides. What they are, what they're not, do they work, and especially are they safe?
- **B** · [0:30](https://www.youtube.com/watch?v=qovnfjVd8cs&t=30s) · Dr. Eric Berg, DC · Is a peptide a powder an injectable? Does it just help you with weight loss? There's just a lot of confusion around it.
- **B** · [0:39](https://www.youtube.com/watch?v=qovnfjVd8cs&t=39s) · Dr. Eric Berg, DC · A peptide is made from amino acids. A peptide is a very short chain, okay, versus like if you were to eat a steak, piece of protein, that's a very long chain.
- **B** · [0:59](https://www.youtube.com/watch?v=qovnfjVd8cs&t=59s) · Dr. Eric Berg, DC · So, look at them like a blueprint.
- **B** · [2:19](https://www.youtube.com/watch?v=qovnfjVd8cs&t=139s) · Dr. Eric Berg, DC · You have a dimmer switch type peptide, or getting the entire new lamp type peptide.
- **B** · [2:40](https://www.youtube.com/watch?v=qovnfjVd8cs&t=160s) · Dr. Eric Berg, DC · When we're young, we produce more than when we get older. This peptide turns the dimmer switch up.
- **B** · [3:23](https://www.youtube.com/watch?v=qovnfjVd8cs&t=203s) · Dr. Eric Berg, DC · It's turned on 24/7 for a week. The signal never shuts off.
- **B** · [4:12](https://www.youtube.com/watch?v=qovnfjVd8cs&t=252s) · Dr. Eric Berg, DC · And the big problem with tendons and ligaments, they have almost no blood supply.
- **B** · [4:41](https://www.youtube.com/watch?v=qovnfjVd8cs&t=281s) · Dr. Eric Berg, DC · For ulcers in the stomach area, sometimes they'll take it for irritable bowel syndrome or leaky gut.

#### 5:00-10:00

- **B** · [5:40](https://www.youtube.com/watch?v=qovnfjVd8cs&t=340s) · Dr. Eric Berg, DC · If you were to get this done, you wouldn't be doing this yourself. You would need a doctor to inject you.
- **B** · [9:26](https://www.youtube.com/watch?v=qovnfjVd8cs&t=566s) · Dr. Eric Berg, DC · Also, if the lab on the COA has no independent website, no phone number, no physical address, you want to be suspect.

#### 10:00-15:00

- **B** · [10:36](https://www.youtube.com/watch?v=qovnfjVd8cs&t=636s) · Dr. Eric Berg, DC · The biggest thing is the type of exercise called sprinting or high-intensity interval training.

## Most Men Will Get Prostate Cancer As They Age \(Here’s What To Do\) (bIvyxp-FAGU)

[Watch on YouTube](https://www.youtube.com/watch?v=bIvyxp-FAGU)

|  | A | B |
| --- | --- | --- |
| Transcript source | youtube\_auto | youtube\_auto |
| Chunk strategy | time:900:60 | time:900:60 |
| Created (UTC) | 2026-11-03 01:08:44 | 2026-11-03 00:53:36 |
| Tokens (in / out) | 10501 / 1727 | 11017 / 2029 |
| Cache read tokens | 0 | 0 |
| Cache write tokens | 0 | 0 |
| Model calls | 3 | 3 |
| Duration (ms) | 903688 | 877788 |
| Cost (USD) | not recorded | not recorded |
| Topics / claims / quotes | 5 / 12 / 8 | 5 / 12 / 10 |
| Unattributed claims | 1 of 12 | 0 of 12 |
| Claim confidence (high / medium / low / not given) | 3 / 9 / 0 / 0 | 9 / 3 / 0 / 0 |
| Speakers coerced to unknown | 0 | 0 |

### Run A

Roster

- Dr. Eric Berg (host)

**TL;DR A:** The video highlights that prostate cancer is extremely common, with statistics showing that up to 60% of men by age 80 have some form of the disease, yet most are not diagnosed because they never die from it. Dr. Eric Berg explains that a high PSA level does not necessarily indicate cancer, as the enzyme can be influenced by factors such as vitamin D levels, insulin, exercise, and ejaculation rather than acting as a reliable cancer marker. He categorizes prostate growth issues into three types—comparing them to a turtle, slow bird, and rabbit—to illustrate that many conditions require little intervention despite their names suggesting urgency. Regarding screening benefits, Berg notes that the estimated life gained from PSA testing is negligible, often only around 37 days, while exposing patients to unnecessary fears and complications like over-diagnosis. Additionally, he suggests maintaining fasting insulin levels under five as part of his daily routine for vitality. Ultimately, the episode argues against the current diagnostic model, emphasizing that the majority of diagnosed cases are benign or slow-growing and do not represent a genuine threat to life.

Topics

- [0:00](https://www.youtube.com/watch?v=bIvyxp-FAGU&t=0s) Prostate Cancer Statistics and Diagnosis
- [0:32](https://www.youtube.com/watch?v=bIvyxp-FAGU&t=32s) Prostate Anatomy and Function
- [2:56](https://www.youtube.com/watch?v=bIvyxp-FAGU&t=176s) Factors Influencing Prostate Growth
- [5:07](https://www.youtube.com/watch?v=bIvyxp-FAGU&t=307s) PSA Testing and Its Limitations
- [7:43](https://www.youtube.com/watch?v=bIvyxp-FAGU&t=463s) Types of Prostate Cancer

### Run B

Roster

- Dr. Eric Berg (host)

**TL;DR B:** Prostate cancer is a significant diagnosis in America, with up to 60% of men having it by age 80, though most cases are not fatal. While the prostate gland wraps around the urethra, conditions like benign prostatic hyperplasia \(BPH\) are common, and PSA is not a definitive cancer marker. Dr. Eric Berg claims that insufficient Vitamin D, high insulin, and elevated DHT can influence prostate size and health. He advises monitoring the velocity of PSA change, noting that a rate greater than 0.75 ng/mL per year could be a red flag. Furthermore, lifestyle recommendations include optimizing Vitamin D, managing insulin, and utilizing practices like fasting.

Topics

- [0:00](https://www.youtube.com/watch?v=bIvyxp-FAGU&t=0s) Prostate Cancer Statistics and Mortality
- [1:08](https://www.youtube.com/watch?v=bIvyxp-FAGU&t=68s) Anatomy and Benign Prostatic Hyperplasia \(BPH\)
- [2:21](https://www.youtube.com/watch?v=bIvyxp-FAGU&t=141s) PSA Test and Prostate Health Factors
- [5:18](https://www.youtube.com/watch?v=bIvyxp-FAGU&t=318s) PSA Test Misconceptions and Causes of Elevated PSA
- [9:31](https://www.youtube.com/watch?v=bIvyxp-FAGU&t=571s) Lifestyle Recommendations for Prostate Health

### Claims

#### 0:00-5:00

- **A** · [0:05](https://www.youtube.com/watch?v=bIvyxp-FAGU&t=5s) · Dr. Eric Berg · medium · Over 300,000 new cases in this year alone.
- **A** · [0:08](https://www.youtube.com/watch?v=bIvyxp-FAGU&t=8s) · Dr. Eric Berg · medium · By age 80, up to 60% of men have prostate cancer and never know it.
- **A** · [0:16](https://www.youtube.com/watch?v=bIvyxp-FAGU&t=16s) · Dr. Eric Berg · medium · Men in their 40s, up to 35% of them have cancer.
- **A** · [0:20](https://www.youtube.com/watch?v=bIvyxp-FAGU&t=20s) · Dr. Eric Berg · medium · When they hit 50, up to 40% of them have cancer.
- **A** · [0:24](https://www.youtube.com/watch?v=bIvyxp-FAGU&t=24s) · Dr. Eric Berg · medium · 60s, it's over 55% of them have cancer.
- **A** · [0:31](https://www.youtube.com/watch?v=bIvyxp-FAGU&t=31s) · Dr. Eric Berg · medium · In their 80s, it goes up to 60%.
- **A** · [0:40](https://www.youtube.com/watch?v=bIvyxp-FAGU&t=40s) · Dr. Eric Berg · high · Almost none of the men that were diagnosed or have cancer will die because of it.
- **A** · [3:52](https://www.youtube.com/watch?v=bIvyxp-FAGU&t=232s) · Dr. Eric Berg · medium · Sufficient vitamin D was able to slow the growth of the prostate by 40% despite what's going on with DHT.
- **A** · [4:24](https://www.youtube.com/watch?v=bIvyxp-FAGU&t=264s) · Dr. Eric Berg · high · Insulin increases something else that will make things grow called insulin-like growth factor.
- **B** · [0:08](https://www.youtube.com/watch?v=bIvyxp-FAGU&t=8s) · Dr. Eric Berg · high · By age 80, up to 60% of men have prostate cancer and never know it.
- **B** · [0:40](https://www.youtube.com/watch?v=bIvyxp-FAGU&t=40s) · Dr. Eric Berg · high · Almost none of the men that were diagnosed or have cancer will die because of it; other things will kill them, but not the prostate cancer.
- **B** · [0:54](https://www.youtube.com/watch?v=bIvyxp-FAGU&t=54s) · Dr. Eric Berg · high · If prostate cancer was as dangerous as the diagnosis rate suggests, most men over 60 would already have been dead by now, but they're not.
- **B** · [2:46](https://www.youtube.com/watch?v=bIvyxp-FAGU&t=166s) · Dr. Eric Berg · high · PSA is not a cancer marker. It can be associated with cancer, but there's many other reasons why the PSA is high.
- **B** · [3:02](https://www.youtube.com/watch?v=bIvyxp-FAGU&t=182s) · Dr. Eric Berg · high · If there's too much DHT, it can actually cause growth of the prostate.
- **B** · [3:50](https://www.youtube.com/watch?v=bIvyxp-FAGU&t=230s) · Dr. Eric Berg · medium · Sufficient vitamin D was able to slow the growth of the prostate by 40% despite what's going on with DHT.
- **B** · [4:26](https://www.youtube.com/watch?v=bIvyxp-FAGU&t=266s) · Dr. Eric Berg · high · Insulin increases something else that will make things grow called insulin-like growth factor.

#### 5:00-10:00

- **A** · [5:39](https://www.youtube.com/watch?v=bIvyxp-FAGU&t=339s) · Unattributed · medium · Prostate cancer was never a cancer marker at all.
- **A** · [9:12](https://www.youtube.com/watch?v=bIvyxp-FAGU&t=552s) · Dr. Eric Berg · medium · The estimated life gained from doing a PSA screening is only 37 days, a little over a month with all the other fears and complications and procedures that end up occurring because of that.
- **A** · [9:51](https://www.youtube.com/watch?v=bIvyxp-FAGU&t=591s) · Dr. Eric Berg · high · You want that \[fasting insulin\] under five.
- **B** · [7:44](https://www.youtube.com/watch?v=bIvyxp-FAGU&t=464s) · Dr. Eric Berg · medium · Number one is called the turtle. It's a cancer that will never turn into a malignancy, it will never be a problem.
- **B** · [7:53](https://www.youtube.com/watch?v=bIvyxp-FAGU&t=473s) · Dr. Eric Berg · high · The great majority of the time \[prostate cancers\] doesn't do anything.
- **B** · [8:49](https://www.youtube.com/watch?v=bIvyxp-FAGU&t=529s) · Dr. Eric Berg · medium · The velocity of greater than 0.75 ng/mL per year would be kind of a red flag.
- **B** · [9:12](https://www.youtube.com/watch?v=bIvyxp-FAGU&t=552s) · Dr. Eric Berg · high · The estimated life gained from doing a PSA screening is only 37 days, a little over a month with all the other fears and complications and procedures that end up occurring because of that.

#### 10:00-15:00

- **A** no claims in this window
- **B** · [10:30](https://www.youtube.com/watch?v=bIvyxp-FAGU&t=630s) · Dr. Eric Berg · high · If someone does have cancer, even the rabbit, there's a gentleman that I interviewed that had prostate cancer that metastasized... What he did, aggressive fasting.

### Quotes

#### 0:00-5:00

- **A** · [0:40](https://www.youtube.com/watch?v=bIvyxp-FAGU&t=40s) · Dr. Eric Berg · Almost none of the men that were diagnosed or have cancer will die because of it.
- **A** · [0:54](https://www.youtube.com/watch?v=bIvyxp-FAGU&t=54s) · Dr. Eric Berg · If prostate cancer was as dangerous as the diagnosis rate suggests, most men over 60 would already have been dead by now, but they're not.
- **A** · [2:46](https://www.youtube.com/watch?v=bIvyxp-FAGU&t=166s) · Dr. Eric Berg · Prostate-specific antigen is not a cancer marker.
- **A** · [4:58](https://www.youtube.com/watch?v=bIvyxp-FAGU&t=298s) · Dr. Eric Berg · This is the routine that I use every day to feel like I'm 18 years old, even though I'm 60 years old.
- **B** · [0:00](https://www.youtube.com/watch?v=bIvyxp-FAGU&t=0s) · Dr. Eric Berg · Prostate cancer is the number one new cancer diagnosis in America right now.
- **B** · [0:16](https://www.youtube.com/watch?v=bIvyxp-FAGU&t=16s) · Dr. Eric Berg · Men in their 40s, up to 35% of them have cancer. When they hit 50, up to 40% of them have cancer. 60s, it's over 55% of them have cancer. In their 70s, it drops down to 50, but in their 80s, it goes up to 60%.
- **B** · [1:09](https://www.youtube.com/watch?v=bIvyxp-FAGU&t=69s) · Dr. Eric Berg · The prostate actually wraps around this little tube that's connected to your bladder called the urethra.
- **B** · [1:27](https://www.youtube.com/watch?v=bIvyxp-FAGU&t=87s) · Dr. Eric Berg · Where basically it's benign, it's not going to be a problem, it just gets bigger over time.
- **B** · [2:23](https://www.youtube.com/watch?v=bIvyxp-FAGU&t=143s) · Dr. Eric Berg · The enzyme responsible for changing into a liquid is called prostate-specific antigen.
- **B** · [2:46](https://www.youtube.com/watch?v=bIvyxp-FAGU&t=166s) · Dr. Eric Berg · PSA is not a cancer marker.
- **B** · [3:07](https://www.youtube.com/watch?v=bIvyxp-FAGU&t=187s) · Dr. Eric Berg · Testosterone turns into DHT with a certain enzyme and there's certain medications and also natural things to inhibit that enzyme, so a person would not get as much DHT and that's supposed to actually help reduce the prostate size.
- **B** · [3:31](https://www.youtube.com/watch?v=bIvyxp-FAGU&t=211s) · Dr. Eric Berg · It can do all of those steps within the prostate cells, so it's not dependent on these other organs because vitamin D is intimately involved in helping regulate the size of the prostate, believe it or not.

#### 5:00-10:00

- **A** · [5:29](https://www.youtube.com/watch?v=bIvyxp-FAGU&t=329s) · Dr. Eric Berg · I'm going to go through the nine reasons why that could be high.
- **A** · [5:43](https://www.youtube.com/watch?v=bIvyxp-FAGU&t=343s) · Unattributed · The scientist said PSA was never a cancer marker at all.
- **A** · [5:59](https://www.youtube.com/watch?v=bIvyxp-FAGU&t=359s) · Dr. Eric Berg · It's just been a nightmare for men, all revolving around this darn PSA test.
- **A** · [7:44](https://www.youtube.com/watch?v=bIvyxp-FAGU&t=464s) · Dr. Eric Berg · Number one is called the turtle. It's a cancer that will never turn into a malignancy, it will never be a problem.
- **B** · [8:36](https://www.youtube.com/watch?v=bIvyxp-FAGU&t=516s) · Dr. Eric Berg · Now, number one, they would want to monitor the velocity or the change of what happens to the PSA number, okay. So over a course of a year, if it goes from two to seven, that's a little bit of a red flag. The velocity of greater than 0.75 ng/mL per year would be kind of a red flag.
- **B** · [9:52](https://www.youtube.com/watch?v=bIvyxp-FAGU&t=592s) · Dr. Eric Berg · I highly recommend you go get a blood test, fasting insulin test. You want that under five.

## The Last EASY Trade Before It Explodes (M8\_DnpYV2hY)

[Watch on YouTube](https://www.youtube.com/watch?v=M8_DnpYV2hY)

|  | A | B |
| --- | --- | --- |
| Transcript source | youtube\_manual | youtube\_manual |
| Chunk strategy | time:900:60 | time:900:60 |
| Created (UTC) | 2026-11-03 01:58:26 | 2026-11-03 02:12:19 |
| Tokens (in / out) | 10144 / 2205 | 10408 / 2358 |
| Cache read tokens | 0 | 0 |
| Cache write tokens | 0 | 0 |
| Model calls | 3 | 3 |
| Duration (ms) | 937565 | 829751 |
| Cost (USD) | not recorded | not recorded |
| Topics / claims / quotes | 2 / 23 / 10 | 3 / 16 / 15 |
| Unattributed claims | 16 of 23 | 16 of 16 |
| Claim confidence (high / medium / low / not given) | 13 / 10 / 0 / 0 | 16 / 0 / 0 / 0 |
| Speakers coerced to unknown | 0 | 0 |

### Run A

Roster

- Brian (host)
- Jensen (unknown)
- Chris Gianno (unknown)

**TL;DR A:** The episode explores the shift from large-scale data center growth to the explosive potential of Edge AI running on devices like phones and robots. While spending behind AI data centers is projected to reach $5 trillion by 2030, Edge AI chips are forecast to grow at a rate north of 30% annually. The discussion highlights specific hardware companies with strong financial performance, noting that Calix's revenue more than doubled after transitioning to a recurring software model, while Nvidia's annual profit multiplied over 40 times in six years. Other highlighted firms include Amborella, which has shipped millions of vision chips and grown revenue by 37%, and Renassus, whose chips deliver massive AI operations with significantly reduced power consumption compared to predecessors. Broadcom is noted for designing 60% of the world's custom AI chips, with its profits increasing eightfold over five years, while Mobileye processes camera frames directly within vehicles installed in more than 230 million cars. The analysis concludes by observing a cyclical market where investors are catching up as these technologies begin to run everywhere on everything owned.

Topics

- [0:00](https://www.youtube.com/watch?v=M8_DnpYV2hY&t=0s) The rise of Edge AI beyond data centers
- [2:16](https://www.youtube.com/watch?v=M8_DnpYV2hY&t=136s) Investment thesis on Edge AI hardware companies

### Run B

Roster

- Brian (host)
- Jensen (unknown)
- Chris Gianno (unknown)

**TL;DR B:** The industry is shifting AI processing from centralized data centers to local devices, known as Edge AI, where intelligence runs directly on the device instead of being sent to the cloud. This trend is poised for explosive growth, with Edge AI chips forecast to grow even faster than the already substantial data center AI market. Companies like Nvidia have seen massive profit increases, while Amborella is noted as a company whose revenue is largely derived from edge AI vision. Specific industry players positioned to benefit include Calix, Broadcom, Mobileye, Renassus, and others. Renassus, for example, developed an automotive chip delivering high AI operations per second while reducing power consumption. The speaker emphasizes that the winner in the Edge AI race is seen as the winner of the overall AI race.

Topics

- [0:00](https://www.youtube.com/watch?v=M8_DnpYV2hY&t=0s) The Shift to Edge AI
- [0:33](https://www.youtube.com/watch?v=M8_DnpYV2hY&t=33s) Market Growth and Opportunity
- [2:17](https://www.youtube.com/watch?v=M8_DnpYV2hY&t=137s) Specific Companies in Edge AI

### Claims

#### 0:00-5:00

- **A** · [0:33](https://www.youtube.com/watch?v=M8_DnpYV2hY&t=33s) · Unattributed · high · The AI data center build out is growing more than 25% a year.
- **A** · [0:43](https://www.youtube.com/watch?v=M8_DnpYV2hY&t=43s) · Unattributed · high · Spending behind AI data centers is heading toward $5 trillion by 2030.
- **A** · [1:01](https://www.youtube.com/watch?v=M8_DnpYV2hY&t=61s) · Brian · medium · Edge AI chips are forecast to grow north of 30% a year.
- **A** · [2:55](https://www.youtube.com/watch?v=M8_DnpYV2hY&t=175s) · Unattributed · high · Calix's revenue has more than doubled since flipping its model to a recurring revenue software platform.
- **A** · [3:03](https://www.youtube.com/watch?v=M8_DnpYV2hY&t=183s) · Unattributed · high · Calix grew its margin from the mid-40s to a record near 57%.
- **A** · [3:30](https://www.youtube.com/watch?v=M8_DnpYV2hY&t=210s) · Unattributed · high · Workers with AI skills are now demanding a 62% wage premium over those who don't.
- **B** · [0:00](https://www.youtube.com/watch?v=M8_DnpYV2hY&t=0s) · Unattributed · high · Agriculture equipment, manufacturing equipment, heavy industry equipment will all be agentic.
- **B** · [0:26](https://www.youtube.com/watch?v=M8_DnpYV2hY&t=26s) · Unattributed · high · The winner of the edge is going to be the winner of the AI race.
- **B** · [0:33](https://www.youtube.com/watch?v=M8_DnpYV2hY&t=33s) · Unattributed · high · The AI data center build out is growing more than 25% a year, and the spending behind it is heading toward $5 trillion by 2030.
- **B** · [1:01](https://www.youtube.com/watch?v=M8_DnpYV2hY&t=61s) · Unattributed · high · Edge AI chips are forecast to grow even faster than the data center, north of 30% a year.
- **B** · [1:28](https://www.youtube.com/watch?v=M8_DnpYV2hY&t=88s) · Unattributed · high · Instead of shipping everything up to a data center and waiting for an answer, the intelligence runs right there on the device.
- **B** · [2:44](https://www.youtube.com/watch?v=M8_DnpYV2hY&t=164s) · Unattributed · high · For its entire first decade as a public company, Calix never even turned a profit.
- **B** · [2:55](https://www.youtube.com/watch?v=M8_DnpYV2hY&t=175s) · Unattributed · high · Calix stopped being a box seller, and it became a recurring revenue software platform.
- **B** · [3:30](https://www.youtube.com/watch?v=M8_DnpYV2hY&t=210s) · Unattributed · high · Workers with AI skills are now demanding a 62% wage premium over those who don't.

#### 5:00-10:00

- **A** · [5:35](https://www.youtube.com/watch?v=M8_DnpYV2hY&t=335s) · Brian · medium · Nvidia's annual profit has multiplied more than 40 times in six years.
- **A** · [5:37](https://www.youtube.com/watch?v=M8_DnpYV2hY&t=337s) · Unattributed · high · Six years ago Nvidia earned under $3 billion, and in its most recent year it earned $120 billion.
- **A** · [6:19](https://www.youtube.com/watch?v=M8_DnpYV2hY&t=379s) · Brian · medium · Amborella's revenue genuinely is edge AI roughly 80% of the time.
- **A** · [6:25](https://www.youtube.com/watch?v=M8_DnpYV2hY&t=385s) · Unattributed · high · Amborella has shipped more than 46 million vision chips into the field.
- **A** · [6:56](https://www.youtube.com/watch?v=M8_DnpYV2hY&t=416s) · Brian · medium · Amborella's revenue grew about 37% in the past year.
- **A** · [7:36](https://www.youtube.com/watch?v=M8_DnpYV2hY&t=456s) · Unattributed · high · Renassus delivers up to 400 trillion AI operations per second on its R-CAR X5H chip.
- **A** · [7:38](https://www.youtube.com/watch?v=M8_DnpYV2hY&t=458s) · Unattributed · high · Renassus pulled roughly 35% less power than the chip before it.
- **A** · [7:47](https://www.youtube.com/watch?v=M8_DnpYV2hY&t=467s) · Unattributed · medium · Bosch and ZF are already designing on top of Renassus's silicon.
- **A** · [7:54](https://www.youtube.com/watch?v=M8_DnpYV2hY&t=474s) · Unattributed · medium · Renassus is running operating margins in the high teens.
- **A** · [8:43](https://www.youtube.com/watch?v=M8_DnpYV2hY&t=523s) · Brian · high · Broadcom designs about 60% of the world's custom AI chips \(ASICs\).
- **A** · [8:51](https://www.youtube.com/watch?v=M8_DnpYV2hY&t=531s) · Unattributed · medium · Broadcom's profit has gone up roughly eight times over the last five years.
- **A** · [8:56](https://www.youtube.com/watch?v=M8_DnpYV2hY&t=536s) · Unattributed · high · Broadcom's operating margin has climbed from 17% to 40%.
- **A** · [9:42](https://www.youtube.com/watch?v=M8_DnpYV2hY&t=582s) · Brian · medium · Mobileye builds the IQ chip that runs camera frames inside the car itself.
- **A** · [9:51](https://www.youtube.com/watch?v=M8_DnpYV2hY&t=591s) · Unattributed · high · More than 230 million vehicles are built with Mobileye's IQ chip installed.
- **B** · [5:33](https://www.youtube.com/watch?v=M8_DnpYV2hY&t=333s) · Unattributed · high · Nvidia's annual profit has multiplied more than 40 times in six years.
- **B** · [5:45](https://www.youtube.com/watch?v=M8_DnpYV2hY&t=345s) · Unattributed · high · Six years ago, \[Nvidia\] earned under $3 billion in a year. And in its most recent year, it earned $120 billion while holding a gross margin above 70%.
- **B** · [6:22](https://www.youtube.com/watch?v=M8_DnpYV2hY&t=382s) · Unattributed · high · Amborella is the rare name where roughly 80% of their revenue genuinely is edge AI.
- **B** · [7:30](https://www.youtube.com/watch?v=M8_DnpYV2hY&t=450s) · Unattributed · high · Renassus just built the first automotive chip ever made on a 3nm process, the R-CAR X5H, and it delivers up to 400 trillion AI operations per second, while pulling roughly 35% less power than the chip before it.
- **B** · [8:43](https://www.youtube.com/watch?v=M8_DnpYV2hY&t=523s) · Unattributed · high · Broadcom is clear building around 60% of the world's custom AI chips.
- **B** · [8:55](https://www.youtube.com/watch?v=M8_DnpYV2hY&t=535s) · Unattributed · high · Over the last five years, Broadcom's profit has gone up roughly eight times, and its operating margin has climbed from 17% to 40%.
- **B** · [9:54](https://www.youtube.com/watch?v=M8_DnpYV2hY&t=594s) · Unattributed · high · IQ is already built into more than 230 million vehicles on the road, which quietly makes Mobileye one of the most widely deployed pieces of AI hardware on the planet.

#### 10:00-15:00

- **A** · [10:09](https://www.youtube.com/watch?v=M8_DnpYV2hY&t=609s) · Brian · medium · Mobileye's revenue is climbing roughly 27% in the most recent quarter.
- **A** · [11:04](https://www.youtube.com/watch?v=M8_DnpYV2hY&t=664s) · Unattributed · high · Orders for Kohu jumped roughly 57% year over year.
- **A** · [11:18](https://www.youtube.com/watch?v=M8_DnpYV2hY&t=678s) · Unattributed · medium · Kohu's recurring side runs nearly 60% of their business.
- **B** · [11:01](https://www.youtube.com/watch?v=M8_DnpYV2hY&t=661s) · Unattributed · high · Orders just jumped roughly 57% year over year for Kohu.

### Quotes

#### 0:00-5:00

- **A** · [0:09](https://www.youtube.com/watch?v=M8_DnpYV2hY&t=9s) · Unattributed · The winner of the edge is going to be the winner of the AI race.
- **A** · [0:13](https://www.youtube.com/watch?v=M8_DnpYV2hY&t=13s) · Jensen · AI is about to run everywhere and on everything that you own.
- **A** · [1:38](https://www.youtube.com/watch?v=M8_DnpYV2hY&t=98s) · Unattributed · It is faster, it is more private, and it does not need the cloud to think.
- **A** · [2:44](https://www.youtube.com/watch?v=M8_DnpYV2hY&t=164s) · Unattributed · Calix never even turned a profit. It lost money every single year.
- **A** · [3:27](https://www.youtube.com/watch?v=M8_DnpYV2hY&t=207s) · Brian · The AI investment is obviously in yourself. Because workers with AI skills are now demanding a 62% wage premium over those who don't.
- **B** · [0:00](https://www.youtube.com/watch?v=M8_DnpYV2hY&t=0s) · Unattributed · Agriculture equipment, manufacturing equipment, heavy industry equipment will all be agentic.
- **B** · [0:26](https://www.youtube.com/watch?v=M8_DnpYV2hY&t=26s) · Unattributed · Whoever wins edge wins the entire AI race.
- **B** · [0:55](https://www.youtube.com/watch?v=M8_DnpYV2hY&t=55s) · Unattributed · But the next chapter, that's going to be the chips that run AI out here in the real world on your devices.
- **B** · [1:28](https://www.youtube.com/watch?v=M8_DnpYV2hY&t=88s) · Unattributed · Instead of shipping everything up to a data center and waiting for an answer, the intelligence runs right there on the device, the chip that's inside your phone, inside your car, the camera on the wall, and of course, the robot that's on the factory floor.
- **B** · [2:44](https://www.youtube.com/watch?v=M8_DnpYV2hY&t=164s) · Unattributed · For its entire first decade as a public company, Calix never even turned a profit.
- **B** · [2:55](https://www.youtube.com/watch?v=M8_DnpYV2hY&t=175s) · Unattributed · It stopped being a box seller, and it became a recurring revenue software platform.
- **B** · [3:26](https://www.youtube.com/watch?v=M8_DnpYV2hY&t=206s) · Unattributed · I think it's pretty obvious that edge AI is the next wave for investments, but an easier AI investment is obviously in yourself.

#### 5:00-10:00

- **A** · [5:35](https://www.youtube.com/watch?v=M8_DnpYV2hY&t=335s) · Brian · The annual profit has multiplied more than 40 times in six years.
- **A** · [5:37](https://www.youtube.com/watch?v=M8_DnpYV2hY&t=337s) · Unattributed · Nvidia's annual profit has multiplied more than 40 times in six years.
- **A** · [5:59](https://www.youtube.com/watch?v=M8_DnpYV2hY&t=359s) · Brian · Amborella is the purest bet there is on edge AI vision.
- **A** · [9:41](https://www.youtube.com/watch?v=M8_DnpYV2hY&t=581s) · Brian · Mobileye builds the IQ chip that is the eyes and reflexes of the car, running every camera frame inside the car itself.
- **B** · [5:06](https://www.youtube.com/watch?v=M8_DnpYV2hY&t=306s) · Unattributed · It's called Jetson-Thor, the on-robot brain, small enough to ride inside a humanoid, carrying about seven times the compute of the chip that it replaced.
- **B** · [6:04](https://www.youtube.com/watch?v=M8_DnpYV2hY&t=364s) · Unattributed · Amborella is the purest bet there is on edge AI vision.
- **B** · [6:10](https://www.youtube.com/watch?v=M8_DnpYV2hY&t=370s) · Unattributed · The chip that lets a camera, a drone, or a robot actually see the world and decide on the device itself with absolutely no trip to the cloud.
- **B** · [7:30](https://www.youtube.com/watch?v=M8_DnpYV2hY&t=450s) · Unattributed · Renassus just built the first automotive chip ever made on a 3nm process, the R-CAR X5H, and it delivers up to 400 trillion AI operations per second, while pulling roughly 35% less power than the chip before it.
- **B** · [8:38](https://www.youtube.com/watch?v=M8_DnpYV2hY&t=518s) · Unattributed · Broadcom designs them for the biggest cloud names on earth.
- **B** · [9:19](https://www.youtube.com/watch?v=M8_DnpYV2hY&t=559s) · Unattributed · The inference happens right inside the box in your house, with a 5G platform built alongside Samsung.
- **B** · [9:54](https://www.youtube.com/watch?v=M8_DnpYV2hY&t=594s) · Unattributed · IQ is already built into more than 230 million vehicles on the road, which quietly makes Mobileye one of the most widely deployed pieces of AI hardware on the planet.

#### 10:00-15:00

- **A** · [10:47](https://www.youtube.com/watch?v=M8_DnpYV2hY&t=647s) · Unattributed · This is cyclical. And right now you're catching it as the cycle turns.
- **B** · [11:43](https://www.youtube.com/watch?v=M8_DnpYV2hY&t=703s) · Unattributed · The most edge true tester out there, and right now we're starting to catch it on the turn.

## United Health Group Stock Drops 50% and Moody’s Lowers US Credit Rating (EfGtEDJyRvk)

[Watch on YouTube](https://www.youtube.com/watch?v=EfGtEDJyRvk)

|  | A | B |
| --- | --- | --- |
| Transcript source | - | youtube\_auto |
| Chunk strategy | - | time:900:60 |
| Created (UTC) | - | 2026-11-03 02:56:26 |
| Tokens (in / out) | - | 19582 / 4536 |
| Cache read tokens | - | 0 |
| Cache write tokens | - | 0 |
| Model calls | - | 5 |
| Duration (ms) | - | 1589944 |
| Cost (USD) | - | not recorded |
| Topics / claims / quotes | - | 7 / 37 / 21 |
| Unattributed claims | - | 36 of 37 |
| Claim confidence (high / medium / low / not given) | - | 22 / 15 / 0 / 0 |
| Speakers coerced to unknown | - | 2 |

### Run A

No analysis for qwen3.5:4b / v1

### Run B

Roster

- Joseph Carlson (host)
- Scott Bessent (unknown)
- Tom Lee (unknown)
- Andrew Witty (unknown)
- Steven Hemsley (unknown)
- Joe Kernan (unknown)

**TL;DR B:** UnitedHealth Group's stock experienced a significant decline, dropping nearly 50% from its peak, which management attributed to underestimating medical costs within Medicare Advantage plans and lowering its 2025 outlook. Concurrently, Moody's downgraded the US government's debt rating, a move some argue is a lagging indicator that may not reflect the full picture of the nation's finances. Despite this, some analysts view the dip in UnitedHealth Group's stock as a buying opportunity, noting the company's massive free cash flow. Separately, discussions covered the significant development costs and quality-focused delays for Grand Theft Auto 6, as well as an analysis of a viral video showing a police officer interacting with an autonomous vehicle. Ultimately, regarding the US credit downgrade, it was suggested that the ratings agencies' assessment is less meaningful for the government than for individual companies.

Topics

- [0:13](https://www.youtube.com/watch?v=EfGtEDJyRvk&t=13s) UnitedHealth Group Stock Performance and Issues
- [1:19](https://www.youtube.com/watch?v=EfGtEDJyRvk&t=79s) US Credit Rating Downgrade by Moody's
- [12:33](https://www.youtube.com/watch?v=EfGtEDJyRvk&t=753s) Predictive Power of Credit Ratings
- [14:22](https://www.youtube.com/watch?v=EfGtEDJyRvk&t=862s) Government Credit Ratings and Market Impact
- [14:00](https://www.youtube.com/watch?v=EfGtEDJyRvk&t=840s) US Credit Rating Downgrade and Fiscal Policy
- [17:02](https://www.youtube.com/watch?v=EfGtEDJyRvk&t=1022s) Grand Theft Auto 6 Development and Quality Focus
- [20:00](https://www.youtube.com/watch?v=EfGtEDJyRvk&t=1200s) Autonomous Vehicle Incidents and AI Interaction

### Claims

#### 0:00-5:00

- **B** · [0:23](https://www.youtube.com/watch?v=EfGtEDJyRvk&t=23s) · Unattributed · high · United Health Group's stock is up 5.6% today, but it still trades at a price of $38, which is down a staggering 40% year-to-date.
- **B** · [0:29](https://www.youtube.com/watch?v=EfGtEDJyRvk&t=29s) · Unattributed · high · Its market cap nearly cut in half after recent events.
- **B** · [0:45](https://www.youtube.com/watch?v=EfGtEDJyRvk&t=45s) · Unattributed · high · This is a company that typically generates enormous amounts of money, doing over $24 billion of free cash flow in the trailing 12 months.
- **B** · [1:03](https://www.youtube.com/watch?v=EfGtEDJyRvk&t=63s) · Unattributed · medium · United Health Group is likely one of the most hated companies in the market by the public and their own customers.
- **B** · [1:21](https://www.youtube.com/watch?v=EfGtEDJyRvk&t=81s) · Unattributed · high · Moody's downgraded the US government debt from its AAA credit rating.
- **B** · [1:30](https://www.youtube.com/watch?v=EfGtEDJyRvk&t=90s) · Scott Bessent · medium · Moody's is a lacking indicator for US financial health and doesn't fully reflect the changes being made.
- **B** · [1:46](https://www.youtube.com/watch?v=EfGtEDJyRvk&t=106s) · Unattributed · medium · Tom Lee's advice right now is to continue buying.
- **B** · [2:30](https://www.youtube.com/watch?v=EfGtEDJyRvk&t=150s) · Unattributed · high · The company has suspended its 2025 outlook saying medical expenditures are now expected to be higher than it had anticipated.
- **B** · [3:13](https://www.youtube.com/watch?v=EfGtEDJyRvk&t=193s) · Unattributed · high · The stock is down by 41% over the trailing one year, from its peak just recently, where it was trading at almost $600 per share and now it trades at $311, going down nearly 50% from its peak.
- **B** · [3:32](https://www.youtube.com/watch?v=EfGtEDJyRvk&t=212s) · Unattributed · medium · Investors need a sour on the company. Pessimism needs to abound.
- **B** · [4:17](https://www.youtube.com/watch?v=EfGtEDJyRvk&t=257s) · Unattributed · high · United Health Group revised its earnings per share projection from $30 per share, which is what it was at the beginning of the year, to now $25 per share, going down around 17% in a single quarter.
- **B** · [4:34](https://www.youtube.com/watch?v=EfGtEDJyRvk&t=274s) · Unattributed · medium · Joe Kernan on CNBC said, they simply underestimated their health expenses.

#### 5:00-10:00

- **B** · [5:34](https://www.youtube.com/watch?v=EfGtEDJyRvk&t=334s) · Unattributed · high · If your company gets this wrong, if United Health Group gives the wrong estimates, it's not the customer that eats the cost, it's the company.
- **B** · [6:31](https://www.youtube.com/watch?v=EfGtEDJyRvk&t=391s) · Unattributed · high · The COVID years... seniors avoided hospitals, surgeries, and preventative care.
- **B** · [7:15](https://www.youtube.com/watch?v=EfGtEDJyRvk&t=435s) · Unattributed · high · Instead, seniors returned in full force in 2023, 2024, and in 2025.

#### 10:00-15:00

- **B** · [10:21](https://www.youtube.com/watch?v=EfGtEDJyRvk&t=621s) · Unattributed · high · I view the stock as meaningfully undervalued today and I think that investors that buy into it will likely do well over the next couple of years.
- **B** · [11:24](https://www.youtube.com/watch?v=EfGtEDJyRvk&t=684s) · Unattributed · medium · Moody's is a lagging indicator. That's what everyone thinks of credit agencies.
- **B** · [13:12](https://www.youtube.com/watch?v=EfGtEDJyRvk&t=792s) · Unattributed · high · The findings of the study indicated that the credit ratings possess a significant degree of predictability and accuracy concerning credit events.
- **B** · [13:24](https://www.youtube.com/watch?v=EfGtEDJyRvk&t=804s) · Unattributed · high · The ratings overall that Moody's and S&amp;P Global give are highly predictive.
- **B** · [13:32](https://www.youtube.com/watch?v=EfGtEDJyRvk&t=812s) · Unattributed · high · Any peer-reviewed study on the subject concludes the same thing that overall there's outliers where S&amp;P Global and Moody's will get things wrong.
- **B** · [13:48](https://www.youtube.com/watch?v=EfGtEDJyRvk&t=828s) · Unattributed · high · Calling these companies simply lagging indicators is not a correct assessment, and that doesn't fit with the actual data.
- **B** · [14:07](https://www.youtube.com/watch?v=EfGtEDJyRvk&t=847s) · Unattributed · high · The stark math on the GOP tax plan doesn't cut the deficit.
- **B** · [14:22](https://www.youtube.com/watch?v=EfGtEDJyRvk&t=862s) · Unattributed · medium · Moody's and S&amp;P Global and Fitch, which all agree that the credit rating has been lowered on the US government over a year's past, would probably upgrade the credit rating of the US government if this ratio went down. if debt to GDP decreased, if the US government was paying 3% on its debt obligations and not seven.
- **B** · [14:22](https://www.youtube.com/watch?v=EfGtEDJyRvk&t=862s) · Unattributed · medium · Moody's and S&amp;P Global and Fitch, which all agree that the credit rating has been lowered on the US government over a year's past, would probably upgrade the credit rating of the US government if this ratio went down.
- **B** · [14:40](https://www.youtube.com/watch?v=EfGtEDJyRvk&t=880s) · Unattributed · high · I don't think that this is going to crash the market.
- **B** · [14:51](https://www.youtube.com/watch?v=EfGtEDJyRvk&t=891s) · Unattributed · high · Moody's and S&amp;B Global and Fitch rating the US government isn't quite as meaningful as when they rate an individual company.

#### 15:00-20:00

- **B** · [15:22](https://www.youtube.com/watch?v=EfGtEDJyRvk&t=922s) · Unattributed · medium · I don't think Moody's had any information that we didn't have on Friday afternoon.
- **B** · [15:29](https://www.youtube.com/watch?v=EfGtEDJyRvk&t=929s) · Unattributed · high · I'd be viewing this as a buying opportunity.
- **B** · [15:53](https://www.youtube.com/watch?v=EfGtEDJyRvk&t=953s) · Unattributed · medium · What Moody's did was reflect what most investors already know.
- **B** · [15:55](https://www.youtube.com/watch?v=EfGtEDJyRvk&t=955s) · Unattributed · medium · I'm assuming today's pullback's going to be really shallow.
- **B** · [16:47](https://www.youtube.com/watch?v=EfGtEDJyRvk&t=1007s) · Unattributed · medium · I believe there's a very good chance that Tom is correct.
- **B** · [18:20](https://www.youtube.com/watch?v=EfGtEDJyRvk&t=1100s) · Unattributed · high · The amount of development costs just alone with Grand Theft Auto 6 has exceeded $1 billion, beating out everything else in digital entertainment.
- **B** · [18:29](https://www.youtube.com/watch?v=EfGtEDJyRvk&t=1109s) · Unattributed · high · This company is literally pushing the boundaries of digital entertainment.
- **B** · [18:34](https://www.youtube.com/watch?v=EfGtEDJyRvk&t=1114s) · Unattributed · high · The whole reason that this company wants to continue delaying the release of the game is because they have such a strict focus on quality.
- **B** · [19:32](https://www.youtube.com/watch?v=EfGtEDJyRvk&t=1172s) · Unattributed · medium · I'm going to go out on a limb here and say I'm one of the few that is completely okay if this game gets delayed because I think it's better for them to focus on quality to create something truly unique and breathtaking...

#### 20:00-25:00

- **B** · [21:11](https://www.youtube.com/watch?v=EfGtEDJyRvk&t=1271s) · Unattributed · medium · So, he's likely talking to a human, but it's just so funny the way that this looks.
- **B** · [21:26](https://www.youtube.com/watch?v=EfGtEDJyRvk&t=1286s) · Unattributed · medium · Now, reportedly, it wasn't the Whimo's fault. It was trying to actually avoid the collision, but couldn't.

### Quotes

#### 0:00-5:00

- **B** · [0:06](https://www.youtube.com/watch?v=EfGtEDJyRvk&t=6s) · Unattributed · Quality compounding machines that have sold off because of irrational fair.
- **B** · [0:23](https://www.youtube.com/watch?v=EfGtEDJyRvk&t=23s) · Unattributed · The stock is up 5.6% today, but it still trades at a price of $38. that is down a staggering 40% year-to- date.
- **B** · [1:21](https://www.youtube.com/watch?v=EfGtEDJyRvk&t=81s) · Unattributed · Moody's downgraded the US government debt from its AAA credit rating.
- **B** · [1:30](https://www.youtube.com/watch?v=EfGtEDJyRvk&t=90s) · Scott Bessent · Moody's is a quote lacking indicator for US financial health and doesn't fully reflect the changes being made.
- **B** · [4:41](https://www.youtube.com/watch?v=EfGtEDJyRvk&t=281s) · Unattributed · To be more specific, this entire event revolves around what's called the Medicare Advantage plan.

#### 5:00-10:00

- **B** · [7:00](https://www.youtube.com/watch?v=EfGtEDJyRvk&t=420s) · Unattributed · Now at the same time, the insurers believed that this was partially permanent.

#### 10:00-15:00

- **B** · [10:01](https://www.youtube.com/watch?v=EfGtEDJyRvk&t=601s) · Unattributed · I believe that this sell-off is overdone.
- **B** · [11:24](https://www.youtube.com/watch?v=EfGtEDJyRvk&t=684s) · Unattributed · Moody's is a lagging indicator. That's what everyone thinks of credit agencies.
- **B** · [12:39](https://www.youtube.com/watch?v=EfGtEDJyRvk&t=759s) · Unattributed · There's actually a number of studies that look at the empirical data of Moody's and S&amp;P Global's credit rating business to see if they are simply lagging indicators or if they're predictive.
- **B** · [13:55](https://www.youtube.com/watch?v=EfGtEDJyRvk&t=835s) · Unattributed · The accuracy ratio assesses the discriminatory power of the credit ratings by comparing the communive accuracy of the ratings to a random model.
- **B** · [14:03](https://www.youtube.com/watch?v=EfGtEDJyRvk&t=843s) · Unattributed · Part of what Moody's incorporated in their credit downgrade was this new bill being passed.
- **B** · [14:13](https://www.youtube.com/watch?v=EfGtEDJyRvk&t=853s) · Unattributed · And this data was not created by Moody's or S&amp;P Global. This is from Piper Sandler and other thirdparty analysis firms.
- **B** · [14:15](https://www.youtube.com/watch?v=EfGtEDJyRvk&t=855s) · Unattributed · This is from Piper Sandler and other thirdparty analysis firms. They're looking at this saying that we're simply not cutting enough.
- **B** · [14:33](https://www.youtube.com/watch?v=EfGtEDJyRvk&t=873s) · Unattributed · if debt to GDP decreased, if the US government was paying 3% on its debt obligations and not seven.
- **B** · [14:44](https://www.youtube.com/watch?v=EfGtEDJyRvk&t=884s) · Unattributed · It's not again new information. This is something that we've seen deteriorate over a long period of time.
- **B** · [14:57](https://www.youtube.com/watch?v=EfGtEDJyRvk&t=897s) · Unattributed · After all, there's just not as many alternatives to the

#### 15:00-20:00

- **B** · [15:05](https://www.youtube.com/watch?v=EfGtEDJyRvk&t=905s) · Unattributed · And in terms of this downgrade, we have people like Tom Lee being asked what investors should do.
- **B** · [15:33](https://www.youtube.com/watch?v=EfGtEDJyRvk&t=933s) · Unattributed · What Moody's has also changed that's not being highlighted here is they had a negative trend over the past couple of years, meaning things were getting worse for the United States and now they have a neutral trend.
- **B** · [17:49](https://www.youtube.com/watch?v=EfGtEDJyRvk&t=1069s) · Unattributed · The last trailer has been viewed almost half a billion times in 24 hours.
- **B** · [18:13](https://www.youtube.com/watch?v=EfGtEDJyRvk&t=1093s) · Unattributed · This company is literally doing something that no other company has ever done before.
- **B** · [18:57](https://www.youtube.com/watch?v=EfGtEDJyRvk&t=1137s) · Unattributed · They have a philosophy of not looking at just next quarter's numbers.

## Google Stock Will Keep Going Higher, Here’s Why (eEE3S-MvzEs): no analysis for either run
