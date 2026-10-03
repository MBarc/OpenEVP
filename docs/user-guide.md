# OpenEVP user guide

This is the long version. If you just want to get going, the
[Quick start](../README.md#quick-start) in the README is three steps. Come back
here when you want to know what a button does or why something happened.

**Contents**

- [Installing](#installing)
- [Getting recordings off your recorder](#getting-recordings-off-your-recorder)
- [Troubleshooting](#troubleshooting)
- [Marking EVPs](#marking-evps)
- [Recording live](#recording-live)
- [Importing from a recorder with only a headphone jack](#importing-from-a-recorder-with-only-a-headphone-jack)
- [Opening WAV and MP3 files](#opening-wav-and-mp3-files)
- [The EVP Library](#the-evp-library)
  - [Folders](#folders)
  - [Right-click menu](#right-click-menu)
  - [Sharing a recording or clip](#sharing-a-recording-or-clip)
  - [Deleting recordings and clips](#deleting-recordings-and-clips)
  - [Moving recordings](#moving-recordings)
  - [Backups](#backups)
  - [Export WAV with marks](#export-wav-with-marks)
- [The player's settings](#the-players-settings)
- [Playback speed](#playback-speed)
- [Enhance](#enhance)
- [Noise reduction](#noise-reduction)
- [Spectrogram](#spectrogram)
- [EVP clips](#evp-clips)
- [Where your marks live](#where-your-marks-live)
- [Known limits](#known-limits)

## Installing

You only do this once per PC.

1. Download `OpenEVP-Setup-<version>.exe` from the
   [Releases](https://github.com/MBarc/OpenEVP/releases/latest) page and run it.
2. The first time, Windows may say *"Windows protected your PC"*. That's
   because the installer isn't code-signed (it doesn't carry a paid
   certificate that tells Windows who made it). Click **More info**, then
   **Run anyway**.
3. Windows asks for permission once. Click **Yes**, and you're done.

The installer sets up everything:

- The OpenEVP app, plus a command-line tool. That's a text-only version for
  people who like typing commands, and you can ignore it.
- The recorder's driver, the piece of Windows that lets the PC talk to the
  recorder. The recorder doesn't need to be plugged in while you install:
  Windows uses the driver whenever you plug it into any USB port. (How that
  works is in the [technical documentation](technical.md#driver-setup).)
- Microsoft's WebView2, which OpenEVP uses to draw its window, if this PC
  doesn't have it yet. Windows 11 already does. An older Windows 10 PC without
  it downloads it during setup, so it needs internet then.

**What you need:** 64-bit (x64) Windows 10, version 2004 or later, or
Windows 11. Windows in "S mode" (a locked-down mode that only allows Microsoft
Store apps) can't run it, and PCs with an ARM chip aren't supported yet.
**Settings → System → About** tells you what you have.

**Uninstalling:** **Settings → Apps → OpenEVP** removes the app, the driver
package and the certificate the installer made. Your PC is back the way it was.

## Getting recordings off your recorder

1. Plug in the recorder and open **OpenEVP** from the Start menu.
2. Click the recorder on the left. Its folders (A to E) and recordings show up.
3. Tick the recordings you want, or the box at the top of the list to get all
   of them, and click **Export**.

By default they go to `Documents\OpenEVP`, with one subfolder per recorder
folder (made the first time you export). Click the path to open it, or
**Change…** to pick another folder.

You can export the same folder twice without making a mess. A recording is
skipped if a file with identical audio is already there, and existing files
are never overwritten. If a *different* recording has the same name, it's
saved as `... (2).dvf`.

The files come off as `.dvf`, Sony's own format, which other programs can't
play. If you want WAV files instead, pick **WAV** as the format before you click
**Export**. For an ICD-ST25 that's an 8000 Hz, 16-bit mono WAV, the exact file
Sony's Digital Voice Editor makes.

Click a recording, or a saved `.dvf` or `.wav` file, to play it and see its
waveform.

OpenEVP only reads from the recorder. Nothing on it is ever changed or deleted.

## Troubleshooting

### The recorder shows as "needs setup"

This can happen if its driver was removed, for example. Click **Set up
recorder** in the app. It does the same driver step the installer does.

### It says the recorder may be stuck

If a transfer gets interrupted, the recorder gives up on it and keeps
answering "busy". **Unplug the USB cable, wait a few seconds, plug it back
in**, and try again. Anything already saved gets skipped, so you won't end up
with doubles.

## Marking EVPs

With a recording loaded in the player at the bottom of the window:

1. Drag across the waveform to select the part you want to mark. Tick **Loop**
   if you want to hear it over and over.
2. Press **M** (or click **★ Mark EVP**).
3. Pick a class, type what you hear, and click **Save**.
   - **A**: clear. Anyone hears the words.
   - **B**: fairly clear. Most people agree on the words.
   - **C**: faint and hard to make out.

The mark shows up as a coloured band on the waveform and in the list below it.
From the list you can play it (▶), change its class or note (✎) or delete it
(✕). Drag its edges to adjust where it starts and ends, and click a note to
edit it right there. Marks save themselves as you go.

Once you've listened all the way through a recording, tick **Reviewed**. The
library uses that to show you what's still left to go through.

A mark belongs to the recording's audio, not to one particular file. If the
same audio exists as a `.dvf` and a WAV, or you've saved it more than once,
marking it in one place marks it everywhere.

## Recording live

Click **Record live** in the sidebar (under On this PC), or **Record live…**
next to Open audio file. The recording view takes over the window until you
close it.

### Choosing an input

Pick the input under **Input**: the PC's built-in microphone, a USB
microphone, a line-in socket or a USB audio adapter. The meter next to it
shows the level, so you can see sound is coming in before you record. Keep
the loud parts out of the red, because that's where the sound clips and gets
distorted.

OpenEVP remembers the input you picked last time. If it isn't plugged in, it
uses Windows' default input and says so.

The recording is the input exactly as it comes in. Windows' and the
browser's "improvements" (echo cancelling, noise suppression, automatic
volume) are turned off, because they can wipe out the very sounds you're
looking for.

### Recording and marking

1. Under **Save into**, pick the library folder for the recording. It starts
   on the folder the EVP Library was showing.
2. Click **● Record**. The waveform and spectrogram scroll by as it records,
   and the time counts up.
3. When you hear something, press **M** (or click **★ Mark**). OpenEVP marks
   the two seconds just before that moment, class C, with the note "Marked
   while recording". You can change the class and note afterwards, like any
   other mark.
4. Click **■ Stop**. The recording is saved and opens in the player, with its
   marks, and the EVP Library shows the folder it's in.

Recordings are named after the moment you pressed Record, for example
`Live 2026-10-03 21-05-09.wav`. OpenEVP never overwrites a file: if that
name is taken, the new one gets a number, like `Live 2026-10-03 21-05-09 (2).wav`.

They're saved as WAV, at the rate the input works at (often 48 kHz), in mono
or stereo depending on the input, 16-bit. An hour of 48 kHz stereo is about
690 MB. If you want an MP3 to share, record first and convert it later.

While recording, the rest of the app waits: the sidebar is greyed out, and
renaming, moving or deleting library folders waits until you stop.

### Listening while you record

Tick **Listen** to hear the input through your speakers or headphones. It's
off every time you open the view, and it's best used with headphones: a
microphone near speakers picks up its own sound and squeals.

**Enhance what I hear** applies the player's Enhance settings (Boost, filters,
Leveler, hum) to what you hear. The saved recording is never enhanced. It's
always the input as it came in.

### If the power goes or the app crashes

OpenEVP writes the recording to disk as it goes, and every few seconds makes
sure what's on disk is a playable file. If the PC loses power or the app
crashes, you lose at most the last few seconds. While recording, the file is
called `<name>.wav.part`. The next time OpenEVP starts, it finishes any such
file, gives it its proper name, adds the marks you made, and tells you.

### When the disk gets full

OpenEVP always keeps at least 500 MB free on the drive. When there's less than
15 minutes of recording left before that, it warns you, and at the limit it
stops by itself and saves the recording. It also stops at 4 GB, the most a WAV
file can hold (about 6 hours of 48 kHz stereo).

### If the input won't open

If Windows is blocking the microphone, OpenEVP says so and offers **Open
microphone settings**. In Settings, under Privacy & security, Microphone, turn
on **Microphone access** and **Let desktop apps access your microphone**.
If another program is using the input, close it and pick the input again.

## Importing from a recorder with only a headphone jack

Some recorders, like the Panasonic RR-DR60, have no USB at all. You can still
get their recordings onto the PC by playing them into it.

1. Run a cable from the recorder's headphone jack to your PC's line-in, or to a
   USB audio adapter. Many recorders have a 3.5 mm jack; some, like the RR-DR60,
   have a smaller 2.5 mm one, so you may need a 2.5 mm to 3.5 mm adapter. A
   line-in works better than a microphone input, which is much more sensitive.
2. Open **Record live** and click **Import from a recorder**. A short version of
   these steps is shown there too.
3. Choose the input the cable goes into. Play a little and set the recorder's
   volume to about the middle: the meter should move well, but stay out of the
   red.
4. Set the recorder's playback speed to normal, and turn voice activation off.
5. Click **● Record** first, then press Play on the recorder, and let it play
   through. Click **■ Stop** when it's done.

With **Start a new file after 3 seconds of silence** ticked (the default),
OpenEVP starts a new file each time the recorder goes quiet between
recordings. Each file is named after when you started, with a number:
`Import 2026-10-03 21-05-09 (1).wav`, `(2)` and so on. Nothing is ever cut
out: the quiet between two recordings stays at the end of the first file, and
the next one starts half a second before its sound. Put the files back to back
and you have exactly what came in.

OpenEVP also saves the whole import as one file, `Import 2026-10-03 21-05-09 (full).wav`,
so if it ever splits in the wrong place, nothing is lost. (When it doesn't
split at all, there's just the one file.) You can change the 3 seconds, or
untick it to get everything in one file.

How it tells a gap from a pause: in the first moments it learns how quiet the
cable is when nothing plays. A recording's own background (room noise, hiss)
is louder than that, so pauses inside a recording never split it. That's why
it helps to press Record before Play. If you press Play first, the first
recording may stay joined to the next one until OpenEVP has heard a real gap.
When in doubt it doesn't split: a recording made of steady sound, with no
quieter background of its own, stays joined to the next one. A short click
when you press a button doesn't get a file of its own either.

Marks work here too: press **M** and the mark goes into the file being
recorded at that moment, and into the whole import's file.

## Opening WAV and MP3 files

**Open audio file…** plays a WAV or an MP3 from anywhere on the PC.

MP3 files work just like WAVs. They play with the same waveform, zoom, loop
and speed, you can mark them, and **Export WAV with marks** and **Export
clips** work on them too (Export WAV with marks writes a WAV).

That includes MP3s saved under another extension, like the `.mpeg` files
WhatsApp Web uses for voice notes and shared clips. OpenEVP reads `.mp3`,
`.mpeg`, `.mpga`, `.mp2` and `.m2a` files. It looks at the start of the file
to check it really is MPEG audio, so an `.mpeg` *video* is ignored.

Marks belong to the audio, so renaming `x.mp3` to `x.mpeg` keeps them. An
MP3's own tags (the title and comment some apps add) are never read as marks.

## The EVP Library

Click **EVP Library** in the sidebar to see every recording you've saved. By
default it shows your Save-to folder. Click the library's folder name to point
it somewhere else, like a shared drive.

Copies of the same recording (a `.dvf` and its WAV, or a WAV saved twice) are
grouped into one row. Each row shows:

- its investigation (the folder it came from);
- its type and length;
- how many class A, B and C EVPs it has;
- whether it's been reviewed.

Click a row to play it, the triangle to see its marks, or a mark to jump
straight to it. The **All / Has EVPs / A / B / C / Not reviewed** buttons
above the list filter it, and the search box finds a file name, an
investigation (folder) name or a note.

### Folders

The library works like File Explorer: a breadcrumb path at the top (the trail
of folders you're in), subfolders first, then that folder's recordings.

- Click a folder row to select it. Double-click it, or press **Enter** while
  it's selected, to open it.
- Click a step in the breadcrumb to jump back up, or press **Backspace** to go
  up one level.
- Folders can go as deep as you like.
- Turn on **All recordings** to swap the folders for one flat list of every
  recording in the library, which you can filter.

**New folder** makes a folder inside the one you're looking at. The name box
starts empty, since the investigation may have been days ago. Type a name and
click **Create**.

The **Rename** and **Delete** buttons above the list work on whatever you've
ticked. With one recording or clip ticked, **Rename** opens the same Rename
box as the right-click menu; with several ticked it's greyed out ("Tick one
recording to rename"). **Delete** deletes the ticked recordings you can see
(see [Deleting recordings and clips](#deleting-recordings-and-clips)). With
nothing ticked, both buttons act on the selected folder instead. If you've
ticked recordings *and* selected a folder, the ticks win. Hover a button and
its tooltip says what it'll act on, or why it's greyed out. They're greyed out
in a second OpenEVP window, while another job is running and while audio is
being saved. Both also work in the **All recordings** view, on the ticked
rows.

Deleting a folder asks first, and tells you what's inside so you don't lose
anything by accident:

- how many recordings, and how many have EVPs (or that not all have been
  checked yet);
- any recorder backups that would go with it (those recordings get **Retry
  backup** afterwards);
- other files, like photos or video;
- subfolders, and the total size.

If it's your Save-to folder, it says so, because exporting will just make it
again.

The folder goes to the **Recycle Bin**. OpenEVP refuses up front, and tells you
to use File Explorer instead, when it can tell Windows wouldn't recycle it:

- on a drive with no Recycle Bin (some network and external drives);
- when the Recycle Bin is set to delete files straight away;
- when the folder is bigger than the Recycle Bin, if Windows reports its size.

If Windows still turns out not to be able to recycle something, OpenEVP stops
it: nothing is ever deleted for good, the folder stays where it was, and OpenEVP
tells you. If a file in the folder is in use, whatever
could be recycled is, the rest stays where it was, and OpenEVP tells you.

### Right-click menu

Right-click in the library for the same tools:

- on an empty part of the list: **New folder**;
- on a folder: **Open**, **Rename** or **Delete…**;
- on a recording: **Play**, **Copy file**, **Rename…**, **Move to…**,
  **Export clips** or **Delete…**. If you right-click a ticked recording,
  **Move to…** and **Delete…** take all the ticked ones.

A folder's **Export clips** does every marked recording in it and in its
subfolders (see [EVP clips](#evp-clips)).

**Show in File Explorer** (on a recording or clip) opens its folder in File
Explorer with the file selected. It picks the file the row is named after, so
the `.dvf` when there's a `.wav` copy too. **Open in File Explorer** (on a
folder, `Clips` folders included) opens that folder.

**Rename…** (or **F2**) renames a recording. Its `.dvf` and `.wav` in that
folder both get the new name and keep their own extension. A name that's
already taken is refused, and the marks stay with the recording.

A second OpenEVP window can't create, rename, delete or move anything in the
library, and can't export. Do those in the first window.

### Sharing a recording or clip

Drag a row out of the library and drop it on Discord, WhatsApp (the app, or
WhatsApp Web in a browser), an email, the desktop or a folder. It arrives as a
file, exactly as if you'd dragged it from File Explorer. Drag a ticked row and
every ticked recording goes at once.

Or right-click the row and choose **Copy file** (**Copy 2 files** with two
ticked, and so on), or press **Ctrl+C** on the row. Then paste with **Ctrl+V**
into the chat or folder.

What arrives depends on the file:

- A WAV, an MP3 or a clip is shared as it is.
- An MP3 saved as `.mpeg` (how WhatsApp Web saves voice notes), `.mpga`,
  `.mp2` or `.m2a` is shared as a copy called `<name>.mp3`, because some apps
  think `.mpeg` means video.
- A recorder's `.dvf` is shared as the `.wav` beside it if there is one and it
  holds the same recording (a different recording that only has the same name
  never goes in its place).
  Otherwise OpenEVP makes an MP3 of the whole recording first. You'll see
  "Preparing … to share" for a moment. Keep holding the mouse button. If you
  let go too early, just drag again and it starts straight away.

Those MP3s live in a temporary folder and are cleared out after a few hours.
Sharing only ever copies. Your original file stays where it is.

### Deleting recordings and clips

Right-click a recording or clip and choose **Delete…**, or select its row and
press the **Delete** key. It goes to the Recycle Bin along with its copies in
that folder (the `.dvf` and its `.wav` go together).

If the row is ticked, every ticked recording you can see goes with it. A
recording hidden by the search, a filter or being in another folder is never
included.

OpenEVP asks first, listing the files and how many EVP marks they carry. The
marks are kept, so if you restore a recording from the Recycle Bin, its marks
are back too. A file that's in use stays where it is, and OpenEVP tells you
which one. If the player has the recording open, it lets go of it first.

### Moving recordings

To move recordings to another folder, tick them and click **Move to…**, or drag
a row onto a folder or a breadcrumb step. (Dropped there, it moves. Dropped
outside OpenEVP, it's shared as a copy, see
[Sharing](#sharing-a-recording-or-clip).)

Marks travel with the audio, so nothing about a recording's marks changes when
it moves. The **Investigation** column always shows the top-level folder a
recording sits in, however deep it's tucked away.

Recorder exports still go to the separate **Save to** folder. Folders in the
library only organise what's already there.

### Backups

The first time you mark a recording that's still on the recorder, OpenEVP
quietly saves a copy of it to the Save-to folder (the `.dvf`, plus a WAV if
it can convert it). That way the mark isn't lost if the recorder gets wiped
later.

A line under the marks list tells you when it's done. If the backup failed, or
couldn't start because the app was closing or updating, the line says it isn't
backed up yet and offers **Retry backup**.

### Export WAV with marks

**Export WAV with marks** saves a WAV copy of the loaded recording with its
marks written into it. It goes to the Save-to folder: into the recorder
folder's letter, or the investigation's folder for a file in the library.

The marks are standard WAV markers (RIFF `cue`, `labl` and `ltxt`, if you're
curious), the kind a lot of audio editors can read. None has been tested with
OpenEVP yet.

## The player's settings

To the right of the waveform, three small tabs hold the player's settings:

- **View:** Zoom, Height, Spectrogram;
- **Speed:** speed, Keep pitch, Exports at …×;
- **Enhance:** the listening aids, Reduce noise, Exports enhanced.

Click a tab, or use the arrow keys on it (**Home** and **End** jump to the
first and last). It remembers the last one you picked.

A tab with something changed from normal shows a dot, and its tooltip says
what changed. That way a setting can't hide behind another tab:

- **Speed •** while the speed isn't 1×;
- **Enhance •** while anything on it is turned on;
- **View •** with the spectrogram off, the height raised or the waveform
  zoomed in past fit-to-width.

The keyboard shortcuts (`[` `]` `\`, **M**, **Space**) work whichever tab is
showing.

## Playback speed

**Speed**, on the Speed tab, goes from 0.25× to 2×. `[` slows down, `]`
speeds up, and `\` or a double-click puts it back to 1×.

With **Keep pitch** ticked (the default), slowed-down speech stays at its
normal pitch. Untick it to hear it like a tape: deeper when it's slower. The
speed and Keep pitch are remembered.

While the speed isn't 1×, the tab shows **Exports at 0.5×** (or whatever the
speed is). With it ticked, the player's **Export WAV with marks**, **Export
clips** and **Save clip** save at that speed, the way you're hearing it.

- It ticks itself when you move the speed off 1×, but not after a restart,
  even though the speed itself is remembered. Untick it if you want
  normal-speed exports.
- The file names end in the speed (`…_0.5x.wav`,
  `…_EVP-A_00m12.4s_0.5x.mp3`, or `…_0.5x-tape.wav` with Keep pitch off).
- The marks move to match, and the half second either side of a clip is slowed
  down with it.
- The library's **Export clips** always saves at normal speed.
- Slowing down a whole recording with Keep pitch takes a few seconds, and a
  progress bar shows how far along it is.

## Enhance

The **Enhance** tab holds the listening aids. They change only what you hear,
live while it plays. The recording and its marks are never touched.

- **Boost** makes it up to +24 dB louder (a lot). A soft limiter stops it
  distorting.
- **Leveler** (Light, Medium or Strong) evens out the volume, bringing quiet
  parts up and loud parts down. Sound engineers call this a compressor.
- **Voice filter** keeps only the range voices sit in, about 300 to 3400 Hz.
- **Cut rumble** turns down everything below about 120 Hz, like handling noise
  and wind.
- **Cut hiss** turns down everything above 5 kHz. An ICD-ST25 recording
  (8 kHz) has nothing up there, so it's greyed out for those.
- **Hum remover** cuts out the buzz from mains electricity at 60 Hz (or 50 Hz,
  the mains frequency in Europe), plus the same buzz repeated at its next
  three harmonics.
- **Reset** turns it all off.

The settings are remembered. While any of them is on, the tab reads
**Enhance •** and an **Enhanced** tag sits above the waveform, so you can't
forget it's on.

Turning any of it on also shows **Exports enhanced** on the tab. Like
**Exports at 0.5×**, it's only ticked when you turn enhancement on in this
session. With it ticked, the player's **Export WAV with marks**, **Export
clips** and **Save clip** save what you hear, named `…_enhanced`
(`…_0.5x_enhanced.wav` with a speed too: the speed goes first, then the
enhancements, same as in the player). The library's **Export clips** always
saves the recording as it was recorded. Saving the same thing twice gives
identical files, so the second time it says "already saved".

An enhanced export matches what you hear to within rounding. It can be a tiny
bit different, because the player works at your sound card's rate and its
Leveler delays the sound by 6 ms, while an export keeps every position exactly
so its marks stay put. The details are in the
[technical documentation](technical.md#enhance-player-and-export).

## Noise reduction

To take steady background noise down (hiss, fans, air conditioning,
traffic):

1. Drag across a stretch with **only** that noise in it, no voices. It needs
   at least a quarter of a second, and a second or two is better.
2. Click **Learn noise** under the waveform.
3. Tick **Reduce noise** on the **Enhance** tab. Hover it to see where the
   noise was learnt from.

OpenEVP then makes a noise-reduced copy of the recording's audio, with a
progress bar and **Cancel** while it works. A 30-minute ICD-ST25 recording
takes about 5 seconds. The player switches over to the copy at the same spot
and keeps playing. The **amount** sets how far the noise goes down: 40% (the
default) lowers it by 12 dB, 100% by 30 dB.

**Keep it low.** Strong noise reduction leaves watery, warbling sounds behind,
and those can sound like whispers or voices. If you only hear an "EVP" with
Reduce noise on, check it again with it off.

The copy is exactly the same length and sample rate as the recording, so
marks, selections, loops, the speed and the other enhancements all work on it
as usual, and the marks still belong to the recording. OpenEVP never treats
the copy as a recording of its own: it never shows in the EVP Library, and it
only lives in OpenEVP's temporary cache.

The learnt noise is kept per recording until you close OpenEVP. Loading
another recording turns Reduce noise off. **Exports enhanced** includes it
(named `…_enhanced`): noise reduction goes first, then the speed, then the
other enhancements, same as in the player.

How it works is in the
[technical documentation](technical.md#noise-reduction).

## Spectrogram

The **Spectrogram** (on the **View** tab) is on unless you untick it. It sits
under the waveform: time runs across, pitch goes up (0 Hz at the bottom,
labelled in kHz), and loudness is colour, from black through purple and red to
pale yellow.

A voice shows up as a stack of bright bands (its harmonics) that bend with the
sounds being made. That's often easier to spot than in the waveform, even under
noise.

- It scrolls and zooms with the waveform, and the cursor, the marks and your
  selection cover it too.
- Untick it and it stays off until you tick it again.
- It never slows down opening a recording. The waveform comes first and the
  spectrogram fills in a moment later.
- A 30-minute ICD-ST25 recording takes under 2 seconds and about 60 MB of
  memory.
- MP3 recordings and clips get one too.

How it's worked out is in the [technical documentation](technical.md#spectrogram).

## EVP clips

**Export clips** (next to Export WAV with marks) saves every mark in the
loaded recording as its own short clip. **Save clip** in a mark's row saves
just that one.

The **Clip format** menu next to Export clips picks **MP3 (for sharing)**, the
default, or **WAV (full quality)**. It's remembered, and the library's Export
clips uses it too.

In the library, right-click a recording, or a folder (every marked recording
in it and its subfolders), and choose **Export clips**. A folder runs in the
background with a progress bar and **Cancel**. Recordings that can't be
decoded are skipped and listed with the reason: no decoder, an ICD-ST10 mode
this version can't play, or a damaged file. When it's finished, a banner says
how many clips were saved (and how many were already there), with **Open
folder**.

To share a clip, see [Sharing a recording or clip](#sharing-a-recording-or-clip).

**What's in a clip**

- Each clip is the mark plus **half a second** either side (less at the very
  start or end of the recording). It's cut from the audio in its own format:
  an ICD-ST25 recording stays 8 kHz mono, an ICD-ST10 one 44.1 kHz stereo (ST)
  or 16 kHz mono (SP). Nothing is resampled.
- MP3 clips are 128 kbps constant bitrate, at the recording's own sample rate.
  The LAME encoder can't go above 64 kbps at 8 kHz, so ICD-ST25 clips are
  64 kbps. The mark (class, time and note) is the clip's ID3 title, the name a
  music player shows, and the note is also its comment. A WAV from somewhere
  else at a rate MP3 doesn't support (say 96 kHz) is converted by the encoder
  to the nearest MP3 rate. Mono and stereo only.
- In a WAV clip, the mark's class and note are written in as a WAV marker,
  like in a WAV with marks.

**Where clips go**

- Clips go into a **`Clips`** folder inside the folder Export WAV with marks
  uses (for example `Save to\A\Clips` or `Save to\Old Mill\Clips`).
- They're named `<recording>_EVP-<class>_<MMmSS.s>s[_<note>].mp3` (or `.wav`),
  for example `001_A_003_EVP-A_00m12.4s.mp3`. The note gets shortened, and any
  characters Windows doesn't allow in file names are left out.
- Nothing is overwritten. A clip already saved with exactly the same contents
  counts as "already there", and a different one gets a numbered name
  ("… (2).wav").

**Clips in the library**

- The EVP Library lists the `Clips` folders OpenEVP makes, with a film icon
  and a **Clips** tag. Open one to see its clips, and click a clip to play it.
- A clip is already an EVP, so it never counts as one. Its marker isn't read
  as a mark, and clips are left out of **Has EVPs** (and the other filters),
  the folder counts, the library total and the EVP chips. **All recordings**
  leaves them out too, since they're copies of parts of recordings.
- You can still mark a clip in the player. The mark is kept, but it doesn't
  count either.
- MP3 clips behave exactly like WAV clips: listed, played and markable, never
  counted.

**Clips from a clip**

Load a clip (WAV or MP3), mark the part you want and use **Export clips** or
**Save clip**, or right-click the clip in the library and choose **Export
clips**. The new clips go into the same `Clips` folder as the clip they came
from (never a `Clips` folder inside it), named after it, for example
`001_A_003_EVP-A_00m12.4s_EVP-A_00m00.8s.mp3`. A folder's Export clips (and
Export clips on a `Clips` folder itself) never cuts clips from clips.

**Managing `Clips` folders**

- Rename or delete a `Clips` folder like any other folder (deleting sends it
  to the Recycle Bin). Renaming, moving or deleting a folder takes its `Clips`
  folder with it.
- Recordings can't be moved into a `Clips` folder. It's for clips only.
- Clips (WAV or MP3) can be moved out of it, and then they're ordinary
  recordings. An MP3 anywhere else in the library is a recording.
- OpenEVP knows its own `Clips` folders by a small hidden file inside them,
  `.openevp-clips`. Delete that file and its clips (WAV or MP3) count as
  ordinary recordings. A folder you named `Clips` yourself is just a folder.

## Where your marks live

Marks and settings (including which folder the library shows) are stored on
this PC in `%APPDATA%\OpenEVP\`. Paste that into File Explorer's address bar to
get there.

- `marks.json`: your EVP marks;
- `settings.json`: your settings;
- `index.json`: OpenEVP's own list of files it has already checked.

Only one OpenEVP window can write at a time. A second window open at the same
time can still browse and play, but can't add, change or delete marks (it says
so) until the first one is closed.

## Known limits

Most of this only matters if something looks wrong. It's here so nobody has
to guess what has and hasn't been tested.

- **Record live and Import:** tested with Chromium's built-in test input and
  generated audio, not yet with a real microphone, a USB audio adapter or a
  real RR-DR60. Recordings stop at 4 GB, the most a WAV file can hold (about
  6 hours of 48 kHz stereo).

- **What's been tested:** on Linux, against one ICD-ST25 with 20 LP-mode
  recordings in folder A (with an owner name set, some dated and some not) and
  folders B to E empty. On Windows 11 the installer set up the driver by itself
  and the app listed all 20 recordings from the same recorder (2026-09-25).
- **Folders B to E:** up to v0.8.2, the download command always asked for
  folder A, so recordings in B to E were reported and not saved (never saved
  wrong). It now asks for the right folder, the way Digital Voice Editor does.
  That's been checked on an ICD-ST10 (folders B and C), not yet on an
  ICD-ST25.
- **ICD-ST25: LP mode only.** Only LP recordings have been checked. The
  recorder's folder list says which mode each recording is in, and a mode
  OpenEVP doesn't know is reported and not saved. SP would be caught this way
  if SP is marked differently (not confirmed), so **record in LP**.
- **ICD-ST10:** tested on one recorder with three recordings (2026-09-29).
  - Its recordings have no owner name, and no date while its clock isn't set,
    so their files are named like `001_A_001_Unknown.dvf`.
  - The length shown before a recording is decoded is worked out from its
    audio frames (small chunks of audio). It's exact for a recording made in
    one go, and each restart inside a recording adds about 0.09 s. The decoded length, shown in the library, is exact.
  - Its memory size isn't known. A recording longer than 32 MB (about an hour
    and a half in ST mode) would be reported, not saved.
  - Recordings made with its clock set haven't been tested.
  - The header of its SP-mode `.dvf` files is OpenEVP's own best guess (the LP
    header with SP's codec, channel and rate fields) until it can be compared
    with an SP file saved by Digital Voice Editor.
  - An SP recording's length before it's downloaded is its size divided by
    2000 bytes a second, which can be off by a few hundredths of a second.
    Once saved, its length is counted from its frames and is exact.
- **Long ICD-ST10 recordings are big:** 92 minutes of 44.1 kHz stereo is
  about 930 MB of WAV.
  - Playback decodes straight into a cache on disk (2 GB). The oldest
    recordings are dropped first, room is made before decoding and again if
    the disk fills up, and a cache left behind by a crash is deleted at the
    next start.
  - The library identifies recordings by their audio (a "fingerprint") without
    holding the audio in memory, but a WAV export or a marked backup holds one
    decoded recording in memory while it's saved.
  - The player draws the waveform from the audio itself only for the first
    2.7 minutes of stereo audio (30 minutes of ICD-ST25 audio). Longer
    recordings are drawn from 400 peaks per second.
  - Decoding takes about 1.4 s of processor time per minute of audio, so about
    two minutes for the longest recording.
- An ICD-ST10 shows up as an ICD-ST25 until you open it, and it uses the same
  driver.
- Recordings in slot 64 and up of a recorder folder's list rely on a guess
  about how the recorder lays that list out. Each one is checked against its
  downloaded data, so if the guess is wrong the download stops rather than
  saving a bad file.
