"""Interactive UI for the context-aware speech pipeline.

Records or accepts a scene (one audio clip, possibly several speakers), runs the
thesis_demo front-end on it, and shows how the scene was understood: every
utterance with its time, voice and role, the counted constraints, and the single
instruction handed to the planner. Executing the resulting plan is delegated to
the running action server via ROS.

Usage in a notebook:

    from vad_ui import run_vad_ui
    run_vad_ui()

The heavy models (Whisper, the planner GGUF) are loaded lazily on first use and
kept for the session.
"""

import base64
import io
import json
import os
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

import ipywidgets as widgets
from IPython.display import display

# The demo code lives in the cloned repository, same lookup as demo_ui.py.
DEMO_MODULE_SEARCH_PATHS = (
    Path("/home/jovyan/libs/cognitive_robot_abstract_machine/pycram/demos"),
    Path(__file__).resolve().parents[2]
    / "cognitive_robot_abstract_machine"
    / "pycram"
    / "demos",
)
for _demo_path in DEMO_MODULE_SEARCH_PATHS:
    if _demo_path.is_dir() and str(_demo_path) not in sys.path:
        sys.path.insert(0, str(_demo_path))

# Imported after the search paths are in place, and light enough to stay global:
# the dialogue vocabulary carries no audio or model dependencies.
from thesis_demo.dialogue.interpreter import Outcome  # noqa: E402
from thesis_demo.audio.diarization import EmbeddingBackend  # noqa: E402
from thesis_demo.dialogue.schema import UtteranceRole  # noqa: E402

WORLD_CONTEXT_FILE_NAME = "world_context.json"
"""File the action server publishes the world context to."""

RUN_DIR_VARIABLE = "NLP_RUN_DIR"
"""Environment variable naming the directory that file lives in."""

DEFAULT_RUN_DIR = "~/nlp-binder-run"
"""Directory used when :data:`RUN_DIR_VARIABLE` is unset."""

EXECUTE_PLAN_ACTION = "execute_plan"
"""Name of the ROS action the executor offers."""

ACTION_SERVER_TIMEOUT_S = 5.0
"""How long to wait for that action server before giving up."""

WORLD_STARTUP_CHECKS = 60
"""How many times to look for the world context after starting the world."""

WORLD_CHECK_INTERVAL_S = 2.0
"""Seconds between those checks."""

PLANNER_MAX_ATTEMPTS = 2
"""Plan generations allowed per click.

A rejected plan comes back with the reason it was rejected, which the model
gets to see, so a second pass usually repairs a step the guard refused. The
library default of one attempt stays untouched, because the recorded
evaluation runs depend on it.
"""

WAVEFORM_ENVELOPE_POINTS = 2000
"""Points the waveform is reduced to; enough for the shape, cheap to draw."""

WAVEFORM_FIGURE_SIZE = (9.5, 2.4)
"""Width and height of the waveform figure, in inches."""

WAVEFORM_DPI = 110
"""Resolution of the waveform figure."""

SEGMENT_LABEL_HEIGHT = 0.92
"""Where a segment label sits, as a fraction of the tallest envelope point."""

WAVEFORM_COLOR = "#7f8c8d"
"""Colour of the waveform itself."""

SEGMENT_COLOR = "#1f4e79"
"""Colour marking a region the voice-activity detector kept."""

SEGMENT_ALPHA = 0.22
"""Opacity of that marking, so the waveform stays visible through it."""

ROLE_BACKGROUNDS = {
    UtteranceRole.COMMAND: "#c6e0b4",
    UtteranceRole.CONTEXT: "#ffe699",
    UtteranceRole.IGNORED: "#f4cccc",
}
"""Row colour per role in the utterance table."""

_state = {"whisper": False, "planner": False, "diarizer": None}


# %% lazy model setup

def _ensure_whisper():
    if not _state["whisper"]:
        from thesis_demo.audio.transcriber import setup_whisper

        setup_whisper()
        _state["whisper"] = True


def _ensure_planner():
    if not _state["planner"]:
        from thesis_demo.planner.llm import is_planner_ready, load_planner

        if not is_planner_ready():
            load_planner()
        _state["planner"] = True


def _ensure_diarizer():
    """Return the speaker separation to use, strongest backend available.

    .. note:: Without the ECAPA backend the pipeline still runs; it just cannot
        merge repeated claims from one voice.
    """
    if _state["diarizer"] is None:
        from thesis_demo.audio.diarization import load_diarizer

        try:
            _state["diarizer"] = load_diarizer(EmbeddingBackend.ECAPA)
        except ModuleNotFoundError:
            _state["diarizer"] = load_diarizer(EmbeddingBackend.MFCC)
    return _state["diarizer"]


# %% world context and execution, via the action server's file interface

def _run_dir():
    return Path(os.environ.get(RUN_DIR_VARIABLE, DEFAULT_RUN_DIR)).expanduser()


def _demo_search_path():
    """Directories holding the demo package, as a PYTHONPATH value."""
    return os.pathsep.join(str(p) for p in DEMO_MODULE_SEARCH_PATHS if p.is_dir())


def start_world():
    """Launch the action server that builds the world and publishes its context.

    It runs as its own process, which does not inherit the search path this
    module added to ``sys.path``, so PYTHONPATH is passed explicitly.
    """
    environment = dict(os.environ)
    existing = environment.get("PYTHONPATH", "")
    search_path = _demo_search_path()
    environment["PYTHONPATH"] = (
        f"{search_path}{os.pathsep}{existing}" if existing else search_path
    )
    _run_dir().mkdir(parents=True, exist_ok=True)
    log_file = _run_dir() / "action_server.log"
    process = subprocess.Popen(
        [sys.executable, "-m", "thesis_demo.action_server"],
        stdout=log_file.open("w"),
        stderr=subprocess.STDOUT,
        env=environment,
    )
    return process, log_file


ACTION_SERVER_MODULE = "thesis_demo.action_server"


def _action_server_is_alive():
    """Whether an action server process is currently running.

    Scans the process table rather than tracking a handle, so a world started
    from an earlier kernel or from a terminal is recognised as well.
    """
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            command = (entry / "cmdline").read_bytes().decode(errors="ignore")
        except OSError:
            # The process ended between listing and reading.
            continue
        if ACTION_SERVER_MODULE in command:
            return True
    return False


def world_is_ready():
    """Whether a running action server has published a world context.

    The context file alone is not enough: it outlives the process that wrote
    it, so a server that died would still look like a running world and every
    plan sent to it would wait forever.
    """
    if not (_run_dir() / WORLD_CONTEXT_FILE_NAME).is_file():
        return False
    return _action_server_is_alive()


def _load_world_context():
    """Read the world the robot acts in, as published by the action server.

    :raises RuntimeError: when the file is missing, meaning no world is running.
    """
    context_file = _run_dir() / WORLD_CONTEXT_FILE_NAME
    if not context_file.is_file():
        raise RuntimeError(
            f"No world context at {context_file}. Start the action server first "
            "(thesis_demo.action_server), it writes the file on startup."
        )
    return json.loads(context_file.read_text())


def _send_plan_for_execution(plan_payload, log):
    """Hand the plan to the action server over the execute_plan ROS action."""
    import rclpy
    from rclpy.action import ActionClient
    from thesis_demo_msgs.action import ExecutePlan

    if not rclpy.ok():
        rclpy.init()
    node = rclpy.create_node("vad_ui_client")
    try:
        client = ActionClient(node, ExecutePlan, EXECUTE_PLAN_ACTION)
        if not client.wait_for_server(timeout_sec=ACTION_SERVER_TIMEOUT_S):
            raise RuntimeError("execute_plan action server not reachable")
        goal = ExecutePlan.Goal()
        goal.plan_json = json.dumps(plan_payload)
        log("Executing plan ...")
        send_future = client.send_goal_async(goal)
        rclpy.spin_until_future_complete(node, send_future)
        result_future = send_future.result().get_result_async()
        rclpy.spin_until_future_complete(node, result_future)
        return json.loads(result_future.result().result.result_json)
    finally:
        node.destroy_node()


# %% rendering

def _utterance_table(utterances, triage):
    rows = []
    interpretation = triage.interpretation
    for index, utterance in enumerate(utterances):
        role = (
            interpretation.role_of(index)
            if interpretation is not None
            else UtteranceRole.IGNORED
        )
        speaker = "?" if utterance.speaker_id is None else str(utterance.speaker_id)
        rows.append(
            f"<tr style='background:{ROLE_BACKGROUNDS[role]}'>"
            f"<td>{index}</td>"
            f"<td>{utterance.start_s:.2f}&ndash;{utterance.end_s:.2f}s</td>"
            f"<td>{speaker}</td>"
            f"<td>{utterance.text}</td>"
            f"<td>{role.value}</td>"
            f"<td>{interpretation.effect_of(index) if interpretation else ''}</td>"
            "</tr>"
        )
    return (
        "<table style='border-collapse:collapse;font-size:13px'>"
        "<tr><th>#</th><th>time</th><th>voice</th><th>heard</th>"
        "<th>role</th><th>effect</th></tr>" + "".join(rows) + "</table>"
    )


def _waveform_html(audio_bytes):
    """Draw the recording with the speech regions marked that were kept.

    Shaded blocks went on to transcription; the gaps were dropped.
    """
    # Figure/FigureCanvasAgg instead of pyplot: no global backend is touched, so
    # this cannot interfere with plotting elsewhere in the notebook.
    from faster_whisper.audio import decode_audio
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    from thesis_demo.audio.vad import SAMPLING_RATE, detect_segments

    waveform = decode_audio(io.BytesIO(audio_bytes), sampling_rate=SAMPLING_RATE)
    segments = detect_segments(waveform)
    duration_s = len(waveform) / SAMPLING_RATE

    # An envelope of about 2000 points draws the same shape as 160k samples.
    step = max(1, len(waveform) // WAVEFORM_ENVELOPE_POINTS)
    envelope = [
        float(max(abs(value) for value in waveform[start : start + step]))
        for start in range(0, len(waveform), step)
    ]
    times = [index * step / SAMPLING_RATE for index in range(len(envelope))]

    figure = Figure(figsize=WAVEFORM_FIGURE_SIZE, dpi=WAVEFORM_DPI)
    FigureCanvasAgg(figure)
    axes = figure.subplots()
    axes.fill_between(times, envelope, [-value for value in envelope],
                      color=WAVEFORM_COLOR, linewidth=0)
    for index, segment in enumerate(segments):
        axes.axvspan(segment.start_s, segment.end_s, color=SEGMENT_COLOR, alpha=SEGMENT_ALPHA)
        axes.text(
            (segment.start_s + segment.end_s) / 2,
            max(envelope) * SEGMENT_LABEL_HEIGHT if envelope else 1.0,
            f"[{index}]",
            ha="center", va="top", fontsize=9, color=SEGMENT_COLOR,
        )
    axes.set_xlim(0, duration_s)
    axes.set_xlabel("time [s]", fontsize=9)
    axes.set_yticks([])
    axes.set_title(
        f"Voice activity: {len(segments)} speech segment(s) in {duration_s:.2f} s",
        fontsize=10,
    )
    for spine in ("top", "right", "left"):
        axes.spines[spine].set_visible(False)

    buffer = io.BytesIO()
    figure.savefig(buffer, format="png", bbox_inches="tight")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return (
        f"<img src='data:image/png;base64,{encoded}' style='max-width:100%'>"
        "<p style='font-size:12px;color:#555;margin-top:2px'>"
        "Shaded = kept as speech and transcribed. Gaps = silence or non-vocal "
        "sound, dropped before transcription.</p>"
    )


def _counts_line(triage):
    counts = triage.counts
    if counts is None or not counts.totals:
        return ""
    parts = [f"{name}: {value:+d}" for name, value in sorted(counts.totals.items())]
    line = "Counted changes: " + ", ".join(parts)
    if counts.final:
        line += "  &rarr;  " + ", ".join(
            f"bring {value} {name}" for name, value in sorted(counts.final.items())
        )
    if counts.assumed_distinct_speakers:
        line += " <i>(speakers unlabelled, identical claims counted as distinct)</i>"
    return line


# %% the UI itself

def _build_recorder():
    """Microphone widget, or None when it cannot be created.

    The browser only releases the microphone after a user gesture, so recording
    is started by the widget's own button. Uploading a prepared clip stays
    available and is the reproducible route for evaluation runs.

    Any failure is swallowed, not just a missing package: ipywebrtc also has to
    load front-end assets, and if that fails the upload path must still work.
    """
    try:
        from ipywebrtc import AudioRecorder, CameraStream

        stream = CameraStream(constraints={"audio": True, "video": False})
        return AudioRecorder(stream=stream, filename="scene", autosave=False)
    except Exception:
        return None


def _wire_world_button(button, log):
    """Start the world on click and report when its context appears."""
    def clicked(_button):
        button.disabled = True

        def work():
            try:
                if world_is_ready():
                    log("World already running.")
                    return
                log("Starting the world, this takes a moment ...")
                _, log_file = start_world()
                for _ in range(WORLD_STARTUP_CHECKS):
                    if world_is_ready():
                        log("World ready.")
                        return
                    time.sleep(WORLD_CHECK_INTERVAL_S)
                log(
                    "World did not report ready in time. Last lines of "
                    f"{log_file}:<br><pre>{log_file.read_text()[-800:]}</pre>"
                )
            except Exception:
                log("Error starting the world &mdash; see below.")
                raise
            finally:
                button.disabled = False

        threading.Thread(target=work, daemon=True).start()

    button.on_click(clicked)


def run_vad_ui():
    header = widgets.HTML("<h3>Context-aware speech understanding</h3>")
    recorder = _build_recorder()
    upload = widgets.FileUpload(
        accept="audio/*,.wav,.webm,.mp3,.ogg",
        multiple=False,
        description="Audio clip",
    )
    world_button = widgets.Button(
        description="Start world", button_style="info"
    )
    understand_button = widgets.Button(
        description="Understand scene", button_style="primary", disabled=True
    )
    execute_button = widgets.Button(
        description="Plan and execute", button_style="success", disabled=True
    )
    status = widgets.HTML(
        "Record a scene or upload a clip to begin."
        if recorder is not None
        else "Upload a recorded scene to begin (ipywebrtc missing, so no "
             "in-browser recording)."
    )
    waveform_area = widgets.HTML("")
    table_area = widgets.HTML("")
    instruction_area = widgets.HTML("")
    plan_area = widgets.Output()

    session = {"audio": None, "triage": None, "context": None}

    def log(message):
        status.value = f"<i>{message}</i>"

    def report(text):
        """Write into the output area from a worker thread.

        ipywidgets warns against entering an Output widget as a context manager
        off the main thread, because the capture is process-wide and can land in
        whichever cell happens to be executing. append_stdout writes to this
        widget explicitly and is safe to call from anywhere.
        """
        plan_area.append_stdout(text if text.endswith("\n") else text + "\n")

    def clear_report():
        plan_area.outputs = ()

    def _accept_audio(audio_bytes, source):
        session["audio"] = audio_bytes
        understand_button.disabled = False
        log(f"{source} received ({len(audio_bytes) / 1024:.0f} kB). "
            "Click 'Understand scene'.")

    def _on_upload(_change):
        if upload.value:
            # ipywidgets 7 hands back a dict, 8 a tuple of file objects.
            content = next(iter(upload.value.values()))["content"] \
                if isinstance(upload.value, dict) else upload.value[0].content
            _accept_audio(bytes(content), "Upload")

    def _on_recording(change):
        if change["new"]:
            _accept_audio(bytes(change["new"]), "Recording")

    def _understand(_button):
        understand_button.disabled = True
        execute_button.disabled = True

        def work():
            try:
                # Drawn before the models load, so the segmentation is visible
                # while the slow part is still running.
                log("Detecting speech regions ...")
                waveform_area.value = _waveform_html(session["audio"])

                log("Loading models (first run takes a while) ...")
                _ensure_whisper()
                _ensure_planner()
                from thesis_demo.dialogue.interpreter import planner_backend
                from thesis_demo.dialogue.scene import understand_scene

                log("Reading world context ...")
                session["context"] = _load_world_context()

                log("Listening to the scene ...")
                utterances, triage = understand_scene(
                    session["audio"],
                    session["context"],
                    planner_backend(),
                    diarizer=_ensure_diarizer(),
                )
                session["triage"] = triage

                table_area.value = _utterance_table(utterances, triage)
                if triage.outcome is Outcome.OK:
                    counts = _counts_line(triage)
                    instruction_area.value = (
                        f"<p>{counts}</p>" if counts else ""
                    ) + (
                        "<p><b>Instruction to planner:</b> "
                        f"<code>{triage.instruction}</code></p>"
                    )
                    execute_button.disabled = False
                    log("Scene understood. Review the table, then execute.")
                elif triage.outcome is Outcome.NO_COMMAND:
                    instruction_area.value = (
                        "<p><b>No command for the robot</b> &mdash; nothing to do.</p>"
                    )
                    log("Scene contained no command.")
                else:
                    instruction_area.value = (
                        f"<p><b>Interpretation failed:</b> {triage.rejection_reason}</p>"
                    )
                    log("Interpretation failed.")
            except Exception:
                log("Error &mdash; see below.")
                report(traceback.format_exc())
            finally:
                understand_button.disabled = False

        threading.Thread(target=work, daemon=True).start()

    def _execute(_button):
        execute_button.disabled = True

        def work():
            try:
                from thesis_demo.planner.llm import InferenceConfiguration
                from thesis_demo.planner.llm import plan as run_planner

                log("Planning ...")
                result = run_planner(
                    session["triage"].instruction,
                    context=session["context"],
                    inference=InferenceConfiguration(
                        max_attempts=PLANNER_MAX_ATTEMPTS
                    ),
                )
                clear_report()
                report(json.dumps(result.payload, indent=2))
                if result.outcome == "plan":
                    outcome = _send_plan_for_execution(result.payload, log)
                    log(f"Execution finished: {outcome.get('status', 'unknown')}")
                elif result.outcome == "clarification":
                    log(f"Planner asks: {result.payload}")
                else:
                    log(f"Planner failed: {result.payload}")
            except Exception:
                log("Error &mdash; see below.")
                report(traceback.format_exc())
            finally:
                execute_button.disabled = False

        threading.Thread(target=work, daemon=True).start()

    _wire_world_button(world_button, log)
    upload.observe(_on_upload, names="value")
    understand_button.on_click(_understand)
    execute_button.on_click(_execute)

    controls = [world_button, upload, understand_button, execute_button]
    if recorder is not None:
        recorder.audio.observe(_on_recording, names="value")
        controls.insert(0, recorder)

    display(
        widgets.VBox(
            [
                header,
                widgets.HBox(controls),
                status,
                waveform_area,
                table_area,
                instruction_area,
                plan_area,
            ]
        )
    )
