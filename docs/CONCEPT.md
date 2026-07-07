# Cat Automation — Concept

## Vision

A camera at the door watches who comes and goes. Using computer vision, the
system recognizes each of our own cats individually, tells them apart from
strangers, and acts on that knowledge: it keeps a timeline of who is in or out,
locks the cat door against foreign cats, scares off intruders, and keeps us
informed.

The north star: **our cats come and go freely and we always know where they
are, while foreign cats are kept out — with no human in the loop.**

## The problem

A cat door is a hole in the house. An ordinary flap lets *any* cat through, and
even microchip- or RFID-gated doors only answer one question ("is this chip on
the allow-list?") the instant a cat is already pressing against the flap. They
don't see the cat approaching, can't distinguish our four-plus cats from each
other, keep no history, and do nothing about a stranger loitering on the step.

We want richer behavior than a gate can offer:

- **Know the household.** Recognize each of our own cats *individually*, not
  just "one of ours vs. not."
- **Keep a record.** Track when each cat enters and leaves, so we can answer
  "is Mittens still out?" or "when did she last come home?"
- **Defend the door.** Stop a foreign cat from entering — proactively, before
  it reaches the flap where possible.
- **Deter intruders.** Discourage strangers that hang around the entrance.
- **Stay informed.** Get notified about the events that matter (a stranger
  appears, a resident comes home late) without watching a live feed.

## Goals

1. **Individual identification.** Recognize each resident cat as a distinct
   individual (4+ cats), and classify anything else as a stranger.
2. **Enter/leave tracking.** Detect and log directional transitions (in vs.
   out) per cat, and maintain a current "who's home" state.
3. **Access control.** Lock the cat door when a foreign cat is present and
   unlock it for recognized residents.
4. **Deterrence.** Play a loud sound to scare foreign cats away from the
   entrance.
5. **Notifications.** Alert the owner about noteworthy events (stranger
   detected, deterrent triggered, resident enter/leave).
6. **Autonomy.** Operate unattended, day and night, without a human making the
   call in real time.

## Non-goals

- **Not a general pet-surveillance product.** This is a single-household
  system for our own door and our own cats, not a shippable product.
- **Not indoor behavior monitoring.** We care about the boundary (the door),
  not what cats do elsewhere in the house or yard.
- **Not other animals.** Dogs, wildlife, and people are out of scope except
  insofar as they must not be mistaken for a resident cat (i.e. they should
  never open the door).
- **No physical harm.** Deterrence means startling an intruder with sound;
  it never means anything that could injure an animal.

## Status & phasing

This is an **early prototype** for our own home, running on a trusted LAN (behind
the household firewall and protected Wi-Fi). It is built in phases, narrow part
first — prove the hard thing before automating the door:

- **Phase 1 (now): see and learn.** Get images off the camera, get clipping and
  background/reference handling working, collect and annotate a dataset, and train
  enough to answer the real question — *can we tell our cats apart at all?* No
  actuation; the door is untouched.
- **Later: act.** Once identification is trustworthy, lean on enter/leave
  tracking, then add the physical responses (lock, sound, light) together with the
  access-decision policy they need.

Everything below describes the *full* system; goals it hasn't reached yet are
future phases, not current claims. Because it lives on a trusted home network, the
prototype uses **no authentication or user management** between its parts — a
deliberate choice to revisit only if it ever leaves the house.

## Core concepts

The domain the system reasons about:

- **Resident cat.** One of our own cats. Each has a stable identity (a name)
  and a learned visual signature. Residents are allowed in and out.
- **Foreign cat.** Any cat that is not a known resident. Foreign cats should
  be kept out and, if lingering, deterred.
- **Identity.** The system's best guess of *which* cat it is looking at,
  together with a confidence. When confidence is high it names the individual;
  when low it falls back to "unknown cat," and the safe default is to treat an
  unknown as foreign for access decisions.
- **Presence / occupancy.** The current in-or-out state of each resident cat,
  derived from the running history of transitions.
- **Event.** A timestamped observation worth recording: a detection, an
  identification, an enter, a leave, a door lock/unlock, a deterrent, a
  notification.
- **Zone & direction.** The door defines an inside and an outside. A crossing
  from one to the other is what turns a detection into an *enter* or *leave*.
- **Deterrent.** An action taken against a foreign cat — first line is locking
  the door; escalation is an audible scare sound.
- **Operating mode.** Whether the system is *collecting* images to learn from or
  *running* autonomously. The owner switches between them (see *How the system
  learns*).
- **Annotation queue.** Images awaiting the owner's label — from a collection
  run, or added automatically in run mode when an identification was uncertain.

## Key capabilities

### 1. Individual cat identification

The heart of the system. From the camera feed it detects that a cat is present
and determines which cat — one of the named residents, or a stranger. It must
cope with the hard parts of the real world: four or more cats that may look
alike, varied poses and angles, day and night, weather, and partial views as a
cat approaches the flap.

Because reliable individual recognition across many similar cats is genuinely
hard, the system is explicit about **confidence**, and its decisions degrade
gracefully: certain → name the cat; uncertain → "unknown"; and every
access decision fails *safe* (an unknown cat does not get let in).

### 2. Enter / leave tracking

Detections at the door are resolved into directional transitions and folded
into a per-cat occupancy state and a household timeline. This answers the
everyday questions — who is home right now, when each cat last came or went,
how long they've been out.

### 3. Access control (door lock)

The system drives a controllable cat door. A recognized resident approaching
from outside gets an unlocked door; a foreign cat gets a locked one. The bias
is toward never trapping or shutting out a resident: safety of our own cats
takes precedence over perfectly excluding a stranger. The precise policy for the
tricky cases — an uncertain identity, or a resident and a stranger at the door
together — is deliberately deferred to the phase where actuation is actually
built (see *Status & phasing*).

### 4. Deterrence (sound)

When a foreign cat is at or near the door — especially one that lingers or
repeatedly tries the flap — the system plays a loud deterrent sound to scare it
off. Deterrence is calibrated to avoid distressing our own cats and to avoid
becoming a nuisance (e.g. not firing endlessly at the same stubborn intruder).

### 5. Notifications

The owner is kept informed of the events that matter — a stranger detected, a
deterrent triggered, a resident coming home or going out — through push
notifications, without needing to watch a live video feed.

### 6. At-a-glance dashboard

The main user-facing app. At a glance: which resident cats are home and which are
out, and when each last crossed ("is Mittens still out?"); a timeline of
enter/leave events; and a log of foreign-cat sightings and any deterrents. It is
also where the household is managed — reviewing and correcting identifications,
annotating images, and teaching the system (see *How the system learns*). It runs
on the compute PC, reached from a browser on the home network, and is separate
from the door device's small camera-setup page. It shows status, events, and
stored snapshots — not a live video feed.

## How it works (at a glance)

This section sketches the shape of the system; the detailed design lives in
`docs/ARCHITECTURE.md`.

The compute is **split across two devices on the home network**:

- **At the door — Raspberry Pi + camera.** A small, always-on edge device
  mounted at the entrance. It does only cheap, local work: capture the video,
  clip it to the door area, run simple motion detection, and — being physically
  at the door — drive whatever actuators are installed (door lock, speaker,
  light). It holds no vision models and makes no recognition decisions; if it
  loses contact with the compute PC it reverts to a safe default (door
  unlocked) rather than guessing.
- **On the network — PC with an NVIDIA GPU.** All the intelligence runs here:
  detecting that a cat is present, identifying *which* cat, resolving direction,
  deciding what to do, and keeping the history. The Pi sends it motion-triggered
  imagery; it returns the decisions that drive the actuators, the record, and
  notifications.

The flow, conceptually: the door camera sees motion → a cat is detected → the
GPU host identifies the individual → the system decides (resident: allow;
foreign: lock and, if needed, deter) → it records the event, updates who's home,
and notifies the owner when warranted.

## How the system learns

Telling 4+ similar cats apart is the hard part, so teaching the system is an
explicit, ongoing activity the owner does through the dashboard — and it is kept
separate from the real-time door loop. The *door* keeps deciding autonomously on
whatever it has already learned; *teaching* is where a human is deliberately in
the loop. It runs as a cycle:

1. **Collection.** The system gathers images of cats at the door into a dataset.
2. **Annotation.** The owner labels them — which resident, a stranger, or not a
   cat at all.
3. **Training.** The owner starts a training run that (re)builds the recognition
   model from the labelled data and, once it checks out, promotes it to live use.
4. **Run.** The system operates autonomously on the trained model. Whenever an
   identification is **uncertain**, that image is dropped into an **annotation
   queue** — so the very cases the system struggles with become the next batch of
   training data. The owner labels the queue, trains again, and accuracy improves
   with use.

The owner can **switch back to collection at any time** — to add a new cat, or to
gather more data when performance drops in some condition (say, at night).
Registering a new resident is just collection + annotation focused on that one
cat. And because there is no model on day one, the very first setup begins in
collection with the door in its safe default until a first model is trained.

## User scenarios

- **A resident comes home.** Mittens walks up to the door at night. The camera
  sees her, she's identified as a resident, the door stays/unlocks, and "Mittens
  came in, 23:14" lands on the timeline. No notification needed unless we asked
  for one.
- **A stranger tries the door.** An unknown cat approaches. It's classified as
  foreign, the door locks, and the owner gets a "stranger at the door" alert.
- **A stranger won't leave.** The foreign cat keeps pawing at the locked flap.
  After it lingers, the deterrent sound plays and it leaves.
- **"Is she still out?"** The owner opens the app and sees each cat's current
  in/out status and when they last crossed.
- **Ambiguous sighting.** A cat is seen but recognition is low-confidence
  (bad angle, darkness). The system treats it as unknown, keeps the door locked
  as the safe default, and drops the image into the annotation queue so it can be
  labelled and improve the next training round — rather than guessing a resident
  in.
- **Teaching a new cat.** A new kitten joins the household. The owner switches to
  collection, lets the system gather images of it over a few days, labels them in
  the dashboard, and starts a training run. The kitten is now a recognized
  resident.

## What good looks like (success criteria)

- **Residents get in reliably.** Our own cats are almost never wrongly shut out
  or startled by the deterrent. (False negatives on residents are the worst
  failure and are minimized first.)
- **Strangers rarely get in.** Foreign cats are kept out the large majority of
  the time; occasional misses are acceptable if the alternative is locking a
  resident out.
- **The timeline is trustworthy.** Enter/leave history is accurate enough to
  rely on for "who's home."
- **It runs itself.** Days pass without human intervention, through night and
  weather.
- **Notifications are signal, not noise.** Alerts fire for things that matter
  and don't train the owner to ignore them.

## Constraints & assumptions

- **Distributed & partially offline-tolerant.** Two devices on the LAN; the
  door device must handle the safety basics if the GPU host or network drops.
- **Real-world door conditions.** Outdoor lighting, night operation, weather,
  and cats approaching at speed and odd angles are the normal case, not edge
  cases.
- **Multi-cat difficulty.** Distinguishing 4+ possibly similar cats individually
  is the central technical risk; the design must tolerate uncertainty rather
  than assume perfect recognition.
- **Safety first.** No action may harm an animal; access decisions fail safe
  for residents.
- **Latency matters at the door.** The lock decision must be fast enough to act
  before or as a cat reaches the flap.

## Open questions

- **Controllable door hardware.** What door mechanism do we lock/unlock, and how
  fast and reliable is it? (Retrofit an existing flap vs. a purpose-built lock.)
- **Re-teaching cadence.** The learning loop (see *How the system learns*) covers
  enrollment and retraining; still open is how often re-teaching is needed as cats
  age or coats change, and how much labelled data a reliable model needs.
- **Directionality.** How do we robustly tell "entering" from "leaving"? We stay
  open to auxiliary non-vision inputs here — a motion sensor, or a flap/beam
  sensor — since *direction* need not be vision-based even though
  *identification* is.
- **Deterrent tuning.** How loud, how often, and how to avoid habituating
  intruders or distressing residents and neighbors.
- **Access-decision policy.** To be settled when actuation is added: how "fail
  safe" resolves the cases where it currently points two ways (lock out an
  uncertain cat vs. never shut out a resident), and how to handle multiple cats in
  frame — e.g. a stranger tailgating a resident.

## Out of scope / possible future ideas

- Recognizing and reacting to non-cat visitors (people, wildlife).
- Per-cat access rules (e.g. curfews, or keeping one cat in for medication).
- Live video access and clip capture/replay of door events.
- Health/behavior insights from long-term movement patterns.
- More than one monitored door or entrance.
