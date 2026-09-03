# Context-aware voice activity detection for human-robot interaction

[![Binder](https://binder.intel4coro.de/badge_logo.svg)](https://binder.intel4coro.de/v2/gh/Dalyoooo/Vad-BA/binder-build?urlpath=lab%2Fworkspaces%2Fnew-workspace)

Speak a household scene out loud. Several people may talk at once about
different things. The robot decides, for every phrase it heard, whether that
phrase is the **instruction**, **context** that changes what the instruction
means, or **noise** to ignore — and then carries out what is left.

The point is the middle case. Classical voice activity detection keeps speech
and drops silence; background speech is either let through as part of the
command or discarded as noise. Neither is right when a second person says
"there is already a fork on the table": that phrase is not the command, it is
not noise either, and it changes how many forks the robot should fetch.

**Open the lab:** the badge above, or

```
https://binder.intel4coro.de/v2/gh/Dalyoooo/Vad-BA/binder-build?urlpath=lab%2Fworkspaces%2Fnew-workspace
```

The branch matters. `binder-build` carries this work; `main` is the untouched
template. If the first launch ends in a timeout, open the link again — the
image has to reach the compute node once, and the second attempt finds it
there.

---

## Try it in one minute

1. Wait for the notebook and the **Desktop** tab (RViz) to appear side by side.
2. Leave **Robot** on `hsrb` and **Environment** on `apartment`.
3. Click **Start world** and wait for `World ready: hsrb in the apartment.`
   This takes about 100 seconds and loads the speech and planning models.
4. Click **Play sample scene**.

Everything after that runs on its own: speech regions, voices, roles, the
counted instruction, the plan, and its execution in RViz. The whole chain takes
a few minutes, most of it the language model on CPU.

The shipped recording holds three phrases by two speakers:

| | said by | words | expected role |
|---|---|---|---|
| 0 | voice 1 | *Put two forks from the drawer on the table.* | instruction |
| 1 | voice 2 | *There is already a fork on the table.* | context, one fork fewer |
| 2 | voice 1 | *The weather is nice today.* | ignored |

Two forks were asked for, one is already there, so the sentence handed to the
planner reads

> Put two forks from the drawer on the table. **Bring exactly 1 Fork.**

and the robot fetches one.

---

## The controls

### Robot and environment

`hsrb` · `pr2` · `tiago` and `apartment` · `kitchen`. Pick before starting the
world; both are locked while a world stands. **Stop world** releases them.

The apartment is the tested combination. Its cutlery drawer holds one fork, one
spoon and one knife, and there are two apples, two mugs and two plates on the
surfaces. There is no glass anywhere.

### Start world / Stop world

**Start world** launches the action server, which builds the semantic world,
publishes it to RViz and writes the world description the language stages read.
It also loads Whisper and the planner model, so the first run afterwards is not
slowed down by that. Wait for `World ready` before doing anything else.

**Stop world** shuts it down. Do this between runs: a previous run leaves
objects where it put them, and the next instruction is then grounded against a
world that has already changed.

### Four ways to give it a scene

All four take the same route afterwards, so it makes no difference which one
you use.

**Record (stops on silence)** — the one to use. It measures the room for half a
second, treats anything three times above that as speech, and ends by itself
once you have stopped talking for 1.5 seconds. Nothing to click twice.

**⏺ and the small player** — manual recording: click to start, click again to
stop. Useful when the automatic end fires too early for you.

**Audio clip** — upload a `.wav`, `.webm`, `.mp3` or `.ogg` file. This is the
reproducible route: the same file gives a comparable run, which is what an
evaluation needs.

**Play sample scene** — the recording shipped in this repository, for showing
the demo where no microphone is available.

### Understand scene / Plan and execute

Both are for going step by step instead of letting the chain run through.

**Understand scene** runs only the speech front-end: speech regions, voices,
roles, counting, and the sentence for the planner. Nothing moves in RViz.

**Plan and execute** takes that sentence, asks the planner for a plan and hands
the plan to the world. Enabled once a scene yielded an instruction.

---

## Reading the output

### Voice activity

The waveform with the kept regions shaded. Gaps are silence or non-vocal sound
and were dropped before transcription — Whisper invents words over silence, so
it only ever sees material where somebody spoke.

### Heard phrases

One row per phrase.

| column | meaning |
|---|---|
| **#** | index, the number the model answers with |
| **Time** | where the phrase sits in the recording |
| **Voice** | which speaker, numbered by order of appearance; `?` when the phrase was too short to judge |
| **Heard** | what Whisper transcribed |
| **Role** | `Instruction`, `Context` or `Ignored`; `—` when nothing was judged at all |
| **Effect** | for context phrases, how the task changes |

### Voice distances

How far apart the voices measured, as cosine distance between speaker
embeddings. `0` is the same direction, `1` unrelated. Green is below the
cutoff and was judged one voice, red above it and judged different voices.

This table is the evidence behind the speaker numbers, not decoration. A
verdict alone says "one voice"; a distance of 0.22 against a cutoff of 0.50
also says the decision was comfortable. A value of 0.49 would be a coin toss
and the same verdict.

### Counted changes and the instruction

`Counted changes: Fork: -1 → bring 1 Fork` is the arithmetic: what each context
phrase asked to change, summed per object type, and the resulting absolute
number where the instruction named one.

**Instruction to planner** is the single sentence the planner receives. This is
where this work ends — everything after it is the inherited planning and
execution half.

### The plan

The raw plan as JSON, and the outcome. `Execution finished: ok` means the robot
carried it out. Anything else names where it stopped.

---

## Worked examples

Record these with the automatic recorder, or upload them as clips.

### One person, one instruction

> *Put a spoon on the table.*

One phrase, role `Instruction`, no counting, plan executed.

### Someone else corrects the count

> *Put two spoons on the table.* — *Oh no, only one.*

The second phrase becomes `Context` with an effect of one spoon fewer. The
sentence reads `… Bring exactly 1 Spoon.` This also works when the same person
says both, which is a self-correction rather than a third party speaking.

### Background chatter

> *Put a fork on the table.* — *The weather is nice today.*

The second phrase is `Ignored` and does not reach the planner.

### The same claim from two people versus twice from one

> *Set the table for five people.* — *I already have one.* — *I already have one.*

Word for word identical phrases, and the count depends on who said them:

| | claims counted | needed |
|---|---|---|
| two speakers, one claim each | twice | 5 − 2 = **3** |
| one speaker, saying it twice | once | 5 − 1 = **4** |

Nothing in the words separates these two cases; only the voice does. That is
why speaker separation is not an extra here but a requirement of the counting.
A repeated claim is recognised by the triple (voice, object type, change), so
one person repeating himself collapses while two people each count.

### Naming a different object

> *Bring me a spoon.* — *No, I already have a spoon, bring me a fork.*

A replacement, not a quantity: the count stays untouched and the second phrase
contributes its own clause instead.

---

## Known limits

These are properties of the approach, not defects to work around.

**Overlapping speech.** Two people talking at once produce one speech region,
one voice and one transcription. The chain assumes phrases that are separated
in time.

**Less than 300 ms between speakers.** Phrases closer than that are merged into
one region, with the same consequence.

**At most one instruction per scene.** The schema cannot express two
simultaneous commands. If two people instruct the robot differently, one of
them is picked.

**Quantities above one are rarely executed.** The counting produces the right
number, but the planner turns "two forks" into two steps that it does not mark
as the same thing, and the world holds one of each cutlery type. Judge the
counting by the sentence it produces, not by whether the robot managed it.

**A transcription error can end the run.** A misheard place name is the worst
case: "from the door" instead of "from the drawer" names a location the world
does not have, and no valid plan exists for it.

**Synthetic speech distorts the measurements.** Two different espeak voices
measured 0.03 apart where two real speakers measure 0.85. Use real recordings.

---

## What runs where

| stage | what it does | how |
|---|---|---|
| recording | ends itself on silence | in the browser |
| segmentation | speech regions | Silero VAD, threshold 0.5, minimum speech 250 ms, minimum silence 300 ms, 200 ms padding |
| transcription | one call per region | faster-whisper `small`, English, beam 3 |
| speaker separation | who spoke which region | ECAPA-TDNN embeddings, cosine distance, agglomerative clustering, cutoff 0.50, minimum 0.3 s per region |
| role assignment | instruction, context or noise | Llama 3.1 8B Instruct against the world description, answer validated against a fixed schema, one correction attempt |
| counting | net change per object type | plain Python, each voice's claim counted once |
| planning and execution | inherited | see `pycram/demos/thesis_demo/planner` in the code repository |

The cutoffs and window lengths are starting points, not calibrated values.
Measured so far: 0.22, 0.23, 0.31 and 0.38 between regions of one voice against
0.84 and 0.86 between two voices. The cutoff sits at 0.50, in the gap. Six
measurements from two recordings are not a calibration.

---

## If something goes wrong

**The launch ends in a timeout.** Open the link again. The image has to be
pulled to the compute node once and that takes longer than the start-up
allowance.

**RViz is not visible.** Use the link above rather than a plain `labpath=` one:
that opens single-document mode, which hides the second tab. Failing that,
switch off `Simple` at the bottom left, and the `Desktop` tab appears.

**Nothing happens after recording.** The first run loads models and takes
minutes; the status line says what it is doing. If it says nothing at all,
check the kernel indicator at the bottom right — a session that ran out of
memory answers with a dead kernel.

**Grounding errors when executing.** A previous run moved things. Stop the
world and start it again.

**Your own voice comes out as two speakers.** Distance above the cutoff. Short
exclamations next to spoken sentences are what does it; the numbers are in the
voice distance table, and the cutoff lives in `audio/diarization.py` in the
code repository.

---

## Repository layout

```
notebooks/demo.ipynb          the entry point, one cell
notebooks/vad_ui.py           the interface, the recorder, the panels
notebooks/assets/             sample recording and the stylesheet
binder/Dockerfile             the image; ARG CRAM_REVISION pins the demo code
default.rviz                  the RViz view the lab opens with
requirements.txt              Python dependencies of the image
```

The speech pipeline itself lives in a second repository and is cloned into the
image at build time, pinned by commit:

```
Dalyoooo/cognitive_robot_abstract_machine, branch binder-build
  pycram/demos/thesis_demo/audio/       segmentation, transcription, speakers
  pycram/demos/thesis_demo/dialogue/    role assignment, validation, counting
  pycram/demos/thesis_demo/planner/     inherited
  pycram/demos/thesis_demo/validation/  inherited
  pycram/demos/thesis_demo/execution/   inherited
```

Changing the pipeline means committing there, pointing `ARG CRAM_REVISION` at
the new commit, and pushing this repository too — the image is cached per
commit of this repository, so a code-only change would otherwise be served from
the previous build.
