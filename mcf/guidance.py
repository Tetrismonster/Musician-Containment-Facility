"""Explanations shown in the read-only explanation field and read by F1.

Each entry has a short explanation, plus an optional longer one added when
beginner guidance is turned on.
"""

from mcf.i18n import _


def _texts() -> dict[str, tuple[str, str]]:
    return {
        "model": (
            _("Model: the AI model that will make the music."),
            _("A model is a large file that has already learned from a lot of music. "
              "You don't need to train it. MCF downloads it once, and after that it runs "
              "on your own computer without the internet. Different models are good at "
              "different things, such as songs with vocals or instrumentals. "
              "Models are added in Preferences, on the Models tab."),
        ),
        "preset": (
            _("Preset: a saved set of settings you can reuse."),
            _("If you find settings you like, save them as a preset and pick them from "
              "this list next time instead of setting everything again."),
        ),
        "style": (
            _("Style description: describe the music you want."),
            _("Write it the way you'd describe a song to a friend: genre, mood, instruments, "
              "tempo and the kind of voice. For example: upbeat funk, slap bass, brass "
              "section, confident female vocals."),
        ),
        "lyrics": (
            _("Lyrics: the words to sing, plus optional tags that control the song. "
              "Press Control+T to insert a tag, or F1 for the tag reference."),
            _("Put section tags on their own line, such as [Verse], [Chorus] or [Bridge]. "
              "You can add details after a colon, such as [Chorus: whispered, add strings]. "
              "Settings for the whole song go at the top in curly brackets, such as "
              "{bpm: 120} or {key: A minor}; these override the controls for this song only. "
              "Leave the lyrics empty, or tick Instrumental only, for music without singing."),
        ),
        "instrumental": (
            _("Instrumental only: make music without any singing."),
            "",
        ),
        "length": (
            _("Length: how long the song should be, in seconds."),
            _("Longer songs take longer to generate. MCF tells you how long it expects "
              "a generation to take before it starts."),
        ),
        "mode": (
            _("Mode: what kind of generation to do."),
            _("New song makes something from scratch. Cover performs an existing song again in a new style. "
              "Extend continues a song. Variation makes a new version of a song. Replace a section redoes "
              "part of a song and keeps the rest. From reference audio borrows the overall sound of a "
              "recording you have the rights to."),
        ),
        "source": (
            _("Source song: the song this edit starts from. Pick one from the library or a file."),
            _("Extend adds music after its end. Variation and Replace a section change it. The original "
              "stays in the library untouched; the result is a new song."),
        ),
        "extend_seconds": (_("Seconds to add: how much new music to add after the end of the source song."), ""),
        "inpaint_range": (
            _("Replace from and until: the part of the song, in seconds, to make again."),
            _("Everything outside this range stays as it was. Use Check lyrics and the library's playback to "
              "find the times: Left and Right arrows move 5 seconds while a song plays."),
        ),
        "variation_amount": (_("How different: how far the variation may move away from the source song."), ""),
        "reference": (
            _("Reference audio: a recording whose overall sound the new song should borrow."),
            _("Only use recordings you have the rights to. The model borrows the general sound, not the melody "
              "or words."),
        ),
        "cover_source": (
            _("Song to cover: the recording whose melody the cover follows."),
            _("MCF analyses it first. The model then sings the song again from scratch, following the original "
              "vocal line, with the voice and instruments your style description asks for."),
        ),
        "cover_title": (_("Original title: used for credits in the library and in saved files."), ""),
        "cover_writers": (_("Songwriters: who wrote the original song. Used for credits."), ""),
        "cover_artist": (_("Original artist: who first recorded the song. Used for credits."), ""),
        "cover_preset": (
            _("Cover preset: sets the checkboxes below in one step. You can change them afterwards."),
            _("Faithful keeps nearly everything. New arrangement frees the tempo, key and chords. Genre swap "
              "lets your style description decide everything except the melody, lyrics and structure. Loose "
              "keeps only the melody and lyrics."),
        ),
        "follow_melody": (_("Melody: always followed. A cover that changes the melody is a different song."), ""),
        "follow_lyrics": (
            _("Original lyrics: when ticked, the analysis puts the original words in the Lyrics field."),
            _("Untick it to write your own words. New words fit best when they have the same number of "
              "syllables as the original lines."),
        ),
        "follow_structure": (
            _("Song structure: when unticked, you can choose which sections to keep in the Sections list."), ""),
        "follow_tempo": (_("Tempo: when unticked, set a new tempo in beats per minute."), ""),
        "follow_key": (_("Key: when unticked, shift the song up or down by semitones, for example to suit a "
                         "different voice."), ""),
        "follow_chords": (
            _("Chord progression: when ticked, the original harmony is kept. Untick it to let the model "
              "reharmonise. ACE-Step hears only the vocal line, so it keeps the chords only when the "
              "instrumentation is followed too."), ""),
        "follow_phrasing": (
            _("Vocal phrasing: how the words sit on the beat. Untick it to let the model sing more freely."), ""),
        "follow_length": (_("Length: when unticked, set a maximum length and the song is shortened."), ""),
        "follow_instrumentation": (
            _("Instrumentation: when ticked, the original arrangement is kept and only the voice changes."),
            _("When it's ticked, describe only the voice in the style description. Asking for a different genre "
              "on top of the original arrangement can make the model leave out the vocals. When it's unticked, "
              "only the separated vocal line reaches the model, and your style description decides the "
              "instruments."),
        ),
        "cover_bpm": (_("New tempo: the tempo for the cover, in beats per minute."), ""),
        "cover_transpose": (_("Change key: how many semitones to move the song. Negative numbers are lower."), ""),
        "cover_sections": (_("Sections to keep: untick sections to leave them out of the cover."), ""),
        "cover_length": (_("Maximum length: the cover is shortened to this many seconds."), ""),
        "cover_analyse": (
            _("Analyse the song: separates the vocals, transcribes the lyrics, and finds the tempo, key, chords "
              "and sections. Control+Shift+A."),
            _("The separated audio exists only while the analysis runs, and is deleted afterwards."),
        ),
        "cover_summary": (_("Analysis result: what MCF found in the song."), ""),
        "profile": (
            _("Artist profile: a sound you trained, applied to this song. None uses the model as it is."),
            _("Profiles are made with the training wizard in Preferences, on the Profiles tab. Only profiles "
              "trained for the selected model are listed."),
        ),
        "profile_strength": (
            _("Profile strength: how strongly the profile's sound is applied. 1 is normal."),
            _("Lower values blend the profile with the model's own sound. Values above 1 can sound distorted."),
        ),
        "language": (
            _("Lyrics language: the language the lyrics are sung in."),
            _("Models sing more reliably when they know the language. You can also type {language: French} "
              "at the top of the lyrics."),
        ),
        "models_dir": (
            _("Models folder: where AI models are stored. They're large, typically 4 to 20 GB each."),
            _("An internal solid-state drive loads models fastest. Avoid USB sticks and network drives."),
        ),
        "export_dir": (_("Exported audio folder: where songs go when you save them with Control+S."), ""),
        "library_dir": (_("Library folder: where the library keeps its songs and database."), ""),
        "install_choice": (_("Model to install: the model MCF downloads now. You can add others later."), ""),
        "verbosity": (_("How much MCF speaks. Errors are always announced."), ""),
        "library_details": (_("Details: everything about the selected song, including its settings and "
                              "credits."), ""),
        "profiles_list": (
            _("Artist profiles: sounds you've trained. They stay encrypted inside the MCF folder."), ""),
        "clean_up": (
            _("Clean up: deletes temporary files and caches that MCF can rebuild. Songs, models and profiles "
              "are never touched."), ""),
        "update_check": (
            _("New versions of MCF: how often MCF checks online for a new version. A new version is announced, "
              "never installed without asking. Help, Check for updates checks right away."),
            _("Updates are signed by the MCF project and checked before they're installed. Only the program "
              "files are replaced. Your settings, library, models and profiles are kept.")),
        "beginner": (_("Beginner explanations: adds background to explanations and to the onboarding "
                       "wizard."), ""),
        "performance_mode": (
            _("Performance mode: how the model uses the graphics card and memory."),
            _("Automatic picks the fastest mode that works on this computer. Graphics card only is fastest but "
              "needs the most graphics memory. Graphics card plus system memory fits bigger models. Compressed "
              "uses 8-bit weights to save memory. Processor only works without a graphics card, very slowly."),
        ),
        "estimates": (
            _("Learned time estimates: MCF times every finished job on this computer to make its estimates more "
              "accurate."),
            _("Only timing numbers and hardware details are recorded, never prompts, lyrics or audio. Nothing "
              "is sent anywhere."),
        ),
        "train_files": (_("Songs: the recordings the profile learns from. At least 8."), ""),
        "train_caption": (_("Description: genre, mood, instruments and voice of the selected song. Optional, "
                            "but it helps the profile respond to descriptions."), ""),
        "train_lyrics": (_("Lyrics of the selected song. Optional, but it helps the profile learn how the "
                           "words are sung."), ""),
        "profile_name": (_("Profile name: how the profile appears in lists and in {profile: } tags."), ""),
        "train_length": (
            _("Training length: how many times training goes through the songs."),
            _("Longer training captures more detail, but too much makes the profile copy the songs instead of "
              "learning their style."),
        ),
        "show_advanced": (
            _("Show advanced settings: show or hide settings for fine control."),
            _("You don't need these to get started. The defaults work well."),
        ),
        "seed": (
            _("Seed: a number that decides the random choices. -1 means a new random seed each time."),
            _("Using the same seed with the same settings gives you the same song again. "
              "This is useful when you like a result and want to change only one thing."),
        ),
        "steps": (
            _("Steps: how many refinement passes the model makes."),
            _("More steps can improve quality but take longer. The model's default is a good start."),
        ),
        "guidance_scale": (
            _("Guidance strength: how closely the model follows your description."),
            _("Higher values follow the description more strictly. Values that are too high "
              "can sound harsh or unnatural."),
        ),
        "generate": (
            _("Generate: start making music. Press Escape or the system-wide cancel shortcut to stop."),
            _("The model is loaded into memory only when you press this, and unloaded when "
              "it finishes, so MCF stays fast while you're setting things up."),
        ),
        "status": (
            _("Status: what MCF is doing right now."),
            "",
        ),
        "explanation": (
            _("Explanation: describes the control you were last on. Press F1 on any control to hear its explanation."),
            "",
        ),
        "library_list": (
            _("Library: every song you've made. Press Space to play or pause, and Control+S to save a copy."),
            _("Songs are added here automatically when a generation finishes, together with "
              "the settings that made them."),
        ),
        "models_list": (
            _("Models: the models installed on this computer."),
            _("Models are large downloads, often several gigabytes. Before downloading, MCF "
              "checks whether your computer can run the model well."),
        ),
    }


def explain(key: str, beginner: bool) -> str:
    short, extra = _texts().get(key, ("", ""))
    if beginner and extra:
        return f"{short}\n\n{extra}"
    return short
