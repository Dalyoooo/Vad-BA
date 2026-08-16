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
import signal
import subprocess
import sys
import threading
import time
import traceback
from html import escape
from pathlib import Path

import ipywidgets as widgets
from IPython.display import Javascript, display

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

WORLD_STARTUP_CHECKS = 180
"""How many times to look for the world context after starting the world.

Generous on purpose: a first start parses the environment and the robot with
cold caches and can take several minutes, and waiting is only ever cut short
here, never in the case that matters, because a server that dies is noticed
right away.
"""

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

UNJUDGED_BACKGROUND = "#e8e8e8"
"""Row colour when the scene was never judged, so no role applies."""

UNJUDGED_LABEL = "&mdash;"
"""Shown in place of a role for those rows."""

_state = {"whisper": False, "planner": False, "diarizer": None}

_load_locks = {name: threading.Lock() for name in _state}
"""One lock per model, so a click during preloading joins the load in progress
instead of starting a second one."""


# %% lazy model setup

def _ensure_whisper():
    with _load_locks["whisper"]:
        if not _state["whisper"]:
            from thesis_demo.audio.transcriber import setup_whisper

            setup_whisper()
            _state["whisper"] = True


def _ensure_planner():
    with _load_locks["planner"]:
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
    with _load_locks["diarizer"]:
        if _state["diarizer"] is None:
            from thesis_demo.audio.diarization import load_diarizer

            try:
                _state["diarizer"] = load_diarizer(EmbeddingBackend.ECAPA)
            except ModuleNotFoundError:
                _state["diarizer"] = load_diarizer(EmbeddingBackend.MFCC)
    return _state["diarizer"]


PRELOADED_MODELS = (
    ("speech recognition", _ensure_whisper),
    ("speaker separation", _ensure_diarizer),
    ("planner", _ensure_planner),
)
"""Models fetched ahead of the first click, with the names shown while waiting."""


def _preload_models(label):
    """Load the heavy models while the world is being set up.

    Together they take minutes to come up. Loading them on first use puts that
    wait behind a button press, where nothing appears to happen; starting them
    here overlaps it with choosing a robot and building the world. The label
    reports what is still missing, so a slow first run is explained rather than
    silent.
    """
    pending = {name for name, _ in PRELOADED_MODELS}
    failures = []
    lock = threading.Lock()

    def render():
        parts = []
        if pending:
            parts.append(f"Loading {', '.join(sorted(pending))} ...")
        elif not failures:
            parts.append("Models ready.")
        parts.extend(failures)
        label.value = f"<i>{'<br>'.join(parts)}</i>"

    def load(name, ensure):
        try:
            ensure()
            problem = None
        except Exception as error:
            problem = f"{name} unavailable: {error}"
        with lock:
            pending.discard(name)
            if problem:
                failures.append(problem)
            render()

    render()
    for name, ensure in PRELOADED_MODELS:
        threading.Thread(target=load, args=(name, ensure), daemon=True).start()


# %% world context and execution, via the action server's file interface

def _run_dir():
    return Path(os.environ.get(RUN_DIR_VARIABLE, DEFAULT_RUN_DIR)).expanduser()


def _demo_search_path():
    """Directories holding the demo package, as a PYTHONPATH value."""
    return os.pathsep.join(str(p) for p in DEMO_MODULE_SEARCH_PATHS if p.is_dir())


ROBOT_CHOICES = ("hsrb", "pr2", "tiago")
"""Robots the world builder can attach.

Fewer than the original demo offers, because each one needs a drive and a
description the speech pipeline's world builder knows about.
"""

ENVIRONMENT_CHOICES = ("apartment", "kitchen")
"""Environments the world builder can furnish and annotate."""

WORLD_SELECTION_VARIABLE = "NLP_WORLD_SELECTION"
"""Environment variable the action server reads its selection from."""


def start_world(robot=ROBOT_CHOICES[0], environment_name=ENVIRONMENT_CHOICES[0]):
    """Launch the action server that builds the world and publishes its context.

    It runs as its own process, which does not inherit the search path this
    module added to ``sys.path``, so PYTHONPATH is passed explicitly. The robot
    and environment travel the same way, because the server picks them up from
    its environment when it builds the world.
    """
    environment = dict(os.environ)
    existing = environment.get("PYTHONPATH", "")
    search_path = _demo_search_path()
    environment["PYTHONPATH"] = (
        f"{search_path}{os.pathsep}{existing}" if existing else search_path
    )
    environment[WORLD_SELECTION_VARIABLE] = json.dumps(
        {"robot": robot, "environment": environment_name}
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


def _action_server_pids():
    """Process ids of the running action servers.

    Reads the process table rather than tracking a handle, so a world started
    from an earlier kernel or from a terminal is found as well.
    """
    pids = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            command = (entry / "cmdline").read_bytes().decode(errors="ignore")
        except OSError:
            # The process ended between listing and reading.
            continue
        if ACTION_SERVER_MODULE in command:
            pids.append(int(entry.name))
    return pids


def _action_server_is_alive():
    return bool(_action_server_pids())


WORLD_SHUTDOWN_CHECKS = 20
"""How many times to look for the world to be gone after asking it to stop."""


def stop_world():
    """End the running world and remove the context it published.

    The context file is what the rest of the interface reads the world from, so
    leaving it behind would describe a world that no longer exists.
    """
    for pid in _action_server_pids():
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            # Already gone.
            pass

    for _ in range(WORLD_SHUTDOWN_CHECKS):
        if not _action_server_is_alive():
            break
        time.sleep(WORLD_CHECK_INTERVAL_S)

    (_run_dir() / WORLD_CONTEXT_FILE_NAME).unlink(missing_ok=True)
    return not _action_server_is_alive()


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
        # Without an interpretation nothing was judged at all. Painting every
        # row as noise would claim a decision the model never made.
        role = interpretation.role_of(index) if interpretation is not None else None
        speaker = "?" if utterance.speaker_id is None else str(utterance.speaker_id)
        rows.append(
            f"<tr style='background:{ROLE_BACKGROUNDS.get(role, UNJUDGED_BACKGROUND)}'>"
            f"<td>{index}</td>"
            f"<td>{utterance.start_s:.2f}&ndash;{utterance.end_s:.2f}s</td>"
            f"<td>{speaker}</td>"
            f"<td>{escape(utterance.text)}</td>"
            f"<td>{role.value if role is not None else UNJUDGED_LABEL}</td>"
            f"<td>{escape(interpretation.effect_of(index)) if interpretation else ''}</td>"
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

AUTO_RECORDER_CONTAINER_ID = "vad-auto-recorder"
"""Element the browser-side recorder attaches its controls to."""

AUTO_RECORDING_NAME = "scene-recording.webm"
"""File the browser writes a finished recording to.

Deliberately not a dotted name: the notebook server refuses to create hidden
files through its file interface, which is the route the recording takes.
"""

AUTO_RECORDING_POLL_S = 0.5
"""Seconds between looks for that file."""

AUTO_RECORDING_CALIBRATION_MS = 500
"""How long the room is measured before speech is judged against it."""

AUTO_RECORDING_NOISE_MULTIPLE = 3.0
"""How far above the measured room noise a signal counts as speech."""

AUTO_RECORDING_MIN_THRESHOLD = 0.012
"""Floor under the threshold, so a silent room does not make it trigger-happy."""

AUTO_RECORDING_SILENCE_HOLD_MS = 1500
"""Quiet time after speech that ends the recording."""

AUTO_RECORDING_MAX_MS = 40000
"""Hard limit, so a stuck microphone cannot record forever."""

SAMPLE_SCENE_NAME = "assets/sample-scene.webm"
"""A recorded scene shipped with the repository.

Three utterances in two voices: a command asking for two forks from the drawer
on the table, a remark that one is already lying there, and a word about the
weather. The command names the object, where it comes from and where it goes,
which is the shape the planner turns into a single transport rather than a
pick-up whose destination it then has to guess. The remark is a plain change in
how many, which is what the counting is built to handle. Lets the demo be shown
where no microphone is available or permitted.
"""


def sample_scene_path():
    return Path(__file__).resolve().parent / SAMPLE_SCENE_NAME


def _auto_recording_path():
    """Where the finished recording lands, next to this module."""
    return Path(__file__).resolve().parent / AUTO_RECORDING_NAME


def _auto_recording_api_path():
    """The same file, named the way the notebook server addresses it."""
    return f"{Path(__file__).resolve().parent.name}/{AUTO_RECORDING_NAME}"


def _auto_recorder_script():
    """Browser-side recorder that ends itself once the talking stops.

    The widget recorder hands over its audio only after a click on stop, so
    nothing here can watch the sound while it is being made. This runs where the
    sound is: it measures the room for a moment, treats anything well above that
    as speech, and stops once it has heard speech followed by a stretch of quiet.
    The finished recording goes back through the notebook server's file
    interface, which the waiting side of this module picks up.
    """
    return f"""
(function () {{
  var container = document.getElementById("{AUTO_RECORDER_CONTAINER_ID}");
  if (!container || container.dataset.wired) return;
  container.dataset.wired = "1";

  var button = document.createElement("button");
  button.textContent = "Record (stops on silence)";
  button.style.cssText = "padding:4px 10px;margin-right:8px;cursor:pointer;";
  var status = document.createElement("span");
  status.style.cssText = "font-style:italic;";
  container.appendChild(button);
  container.appendChild(status);

  // The page carries its own address in the notebook server's config block.
  // Guessing "/" instead lands on the hub in front of the server, which
  // answers the upload with a redirect to its login page.
  var configElement = document.getElementById("jupyter-config-data");
  var baseUrl = "/";
  if (configElement) {{
    try {{
      baseUrl = JSON.parse(configElement.textContent).baseUrl || baseUrl;
    }} catch (error) {{
      baseUrl = document.body.dataset.baseUrl || baseUrl;
    }}
  }} else if (document.body.dataset.baseUrl) {{
    baseUrl = document.body.dataset.baseUrl;
  }}

  function xsrfToken() {{
    var match = document.cookie.match(/\\b_xsrf=([^;]+)/);
    return match ? decodeURIComponent(match[1]) : "";
  }}

  function upload(blob) {{
    return new Promise(function (resolve, reject) {{
      var reader = new FileReader();
      reader.onloadend = function () {{
        fetch(baseUrl + "api/contents/{_auto_recording_api_path()}", {{
          method: "PUT",
          headers: {{
            "Content-Type": "application/json",
            "X-XSRFToken": xsrfToken()
          }},
          body: JSON.stringify({{
            type: "file",
            format: "base64",
            content: String(reader.result).split(",")[1]
          }})
        }}).then(function (response) {{
          // A refused upload still resolves, so without this check a lost
          // recording would be reported as a delivered one.
          if (response.ok) {{
            resolve();
          }} else {{
            reject(new Error("server answered " + response.status));
          }}
        }}, reject);
      }};
      reader.readAsDataURL(blob);
    }});
  }}

  button.onclick = function () {{
    button.disabled = true;
    navigator.mediaDevices.getUserMedia({{audio: true}}).then(function (stream) {{
      var recorder = new MediaRecorder(stream);
      var chunks = [];
      var audioContext = new (window.AudioContext || window.webkitAudioContext)();
      var analyser = audioContext.createAnalyser();
      analyser.fftSize = 1024;
      audioContext.createMediaStreamSource(stream).connect(analyser);
      var samples = new Float32Array(analyser.fftSize);

      recorder.ondataavailable = function (event) {{ chunks.push(event.data); }};
      recorder.onstop = function () {{
        stream.getTracks().forEach(function (track) {{ track.stop(); }});
        audioContext.close();
        status.textContent = "sending ...";
        upload(new Blob(chunks, {{type: recorder.mimeType}})).then(function () {{
          status.textContent = "sent; the pipeline takes over.";
          button.disabled = false;
        }}, function (error) {{
          status.textContent = "could not send: " + error;
          button.disabled = false;
        }});
      }};

      var roomNoise = [];
      var threshold = null;
      var heardSpeech = false;
      var quietSince = null;
      var startedAt = performance.now();

      recorder.start();
      status.textContent = "measuring the room ...";

      var timer = setInterval(function () {{
        analyser.getFloatTimeDomainData(samples);
        var total = 0;
        for (var i = 0; i < samples.length; i++) total += samples[i] * samples[i];
        var level = Math.sqrt(total / samples.length);
        var now = performance.now();

        if (now - startedAt < {AUTO_RECORDING_CALIBRATION_MS}) {{
          roomNoise.push(level);
          return;
        }}
        if (threshold === null) {{
          roomNoise.sort(function (a, b) {{ return a - b; }});
          var middle = roomNoise[Math.floor(roomNoise.length / 2)] || 0;
          threshold = Math.max(middle * {AUTO_RECORDING_NOISE_MULTIPLE},
                               {AUTO_RECORDING_MIN_THRESHOLD});
          status.textContent = "listening ...";
        }}

        if (level > threshold) {{
          heardSpeech = true;
          quietSince = null;
          status.textContent = "speech ...";
        }} else if (heardSpeech) {{
          if (quietSince === null) {{
            quietSince = now;
            status.textContent = "quiet ...";
          }} else if (now - quietSince > {AUTO_RECORDING_SILENCE_HOLD_MS}) {{
            clearInterval(timer);
            recorder.stop();
            return;
          }}
        }}

        if (now - startedAt > {AUTO_RECORDING_MAX_MS}) {{
          clearInterval(timer);
          recorder.stop();
        }}
      }}, 50);
    }}, function (error) {{
      status.textContent = "no microphone: " + error;
      button.disabled = false;
    }});
  }};
}})();
"""


def _watch_for_auto_recording(on_audio):
    """Hand over recordings the browser drops off, once they are complete.

    The file is only read after its size has stopped changing, so a recording
    still being written is never passed on half finished.
    """
    path = _auto_recording_path()
    path.unlink(missing_ok=True)

    def work():
        while True:
            time.sleep(AUTO_RECORDING_POLL_S)
            try:
                if not path.is_file():
                    continue
                size = path.stat().st_size
                time.sleep(AUTO_RECORDING_POLL_S)
                if not path.is_file() or path.stat().st_size != size:
                    continue
                audio = path.read_bytes()
                path.unlink(missing_ok=True)
            except OSError:
                continue
            if audio:
                on_audio(audio)

    threading.Thread(target=work, daemon=True).start()


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


def _wire_world_button(button, log, selectors, stop_button):
    """Start the world on click and report when its context appears.

    While a world stands, the selectors are locked, so what they show is always
    what is actually running.
    """
    robot_choice, environment_choice = selectors

    def set_locked(locked):
        robot_choice.disabled = locked
        environment_choice.disabled = locked
        stop_button.disabled = not locked

    def clicked(_button):
        button.disabled = True

        def work():
            try:
                if world_is_ready():
                    log("World already running. Stop it to pick another one.")
                    set_locked(True)
                    return
                log(
                    f"Starting {robot_choice.value} in the "
                    f"{environment_choice.value}, this takes a moment ..."
                )
                process, log_file = start_world(
                    robot=robot_choice.value,
                    environment_name=environment_choice.value,
                )
                for _ in range(WORLD_STARTUP_CHECKS):
                    if world_is_ready():
                        log(
                            f"World ready: {robot_choice.value} in the "
                            f"{environment_choice.value}."
                        )
                        set_locked(True)
                        return
                    if process.poll() is not None:
                        # The server gave up; its log says why, so stop waiting.
                        break
                    time.sleep(WORLD_CHECK_INTERVAL_S)
                log(
                    "World did not come up. Last lines of "
                    f"{log_file}:<br><pre>{log_file.read_text()[-800:]}</pre>"
                )
            except Exception:
                log("Error starting the world &mdash; see below.")
                raise
            finally:
                button.disabled = False

        threading.Thread(target=work, daemon=True).start()

    def stop_clicked(_button):
        stop_button.disabled = True

        def work():
            try:
                log("Stopping the world ...")
                if stop_world():
                    log("World stopped. Pick a robot and an environment.")
                    set_locked(False)
                else:
                    log("World did not stop; it is still running.")
                    set_locked(True)
            except Exception:
                log("Error stopping the world &mdash; see below.")
                raise

        threading.Thread(target=work, daemon=True).start()

    button.on_click(clicked)
    stop_button.on_click(stop_clicked)
    # A world from an earlier kernel is already standing; match the controls.
    set_locked(world_is_ready())


def run_vad_ui():
    header = widgets.HTML("<h3>Context-aware speech understanding</h3>")
    recorder = _build_recorder()
    upload = widgets.FileUpload(
        accept="audio/*,.wav,.webm,.mp3,.ogg",
        multiple=False,
        description="Audio clip",
    )
    robot_choice = widgets.ToggleButtons(
        options=ROBOT_CHOICES,
        value=ROBOT_CHOICES[0],
        description="Robot",
    )
    environment_choice = widgets.ToggleButtons(
        options=ENVIRONMENT_CHOICES,
        value=ENVIRONMENT_CHOICES[0],
        description="Environment",
    )
    world_button = widgets.Button(
        description="Start world", button_style="info"
    )
    stop_world_button = widgets.Button(
        description="Stop world", button_style="warning", disabled=True
    )
    sample_button = widgets.Button(
        description="Play sample scene",
        tooltip="Run the shipped recording through the whole chain",
        disabled=not sample_scene_path().is_file(),
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
    auto_recorder = widgets.HTML(
        f"<div id='{AUTO_RECORDER_CONTAINER_ID}'></div>"
    )
    models_status = widgets.HTML("")
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

    def _accept_audio(audio_bytes, source, next_step="Click 'Understand scene'."):
        session["audio"] = audio_bytes
        understand_button.disabled = False
        log(f"{source} received ({len(audio_bytes) / 1024:.0f} kB). {next_step}")

    def _on_upload(_change):
        if upload.value:
            # ipywidgets 7 hands back a dict, 8 a tuple of file objects.
            content = next(iter(upload.value.values()))["content"] \
                if isinstance(upload.value, dict) else upload.value[0].content
            _accept_audio(bytes(content), "Upload")

    def _on_recording(change):
        if change["new"]:
            _accept_audio(bytes(change["new"]), "Recording")

    def _understand_work():
        """Run the speech front-end. True when an instruction came out of it."""
        understood = False
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
                understood = True
                log("Scene understood.")
            elif triage.outcome is Outcome.NO_COMMAND:
                instruction_area.value = (
                    "<p><b>No command for the robot</b> &mdash; nothing to do.</p>"
                )
                log("Scene contained no command.")
            else:
                # Both the reason and the answer are the model's own words and
                # carry angle brackets, which vanish when shown as markup.
                instruction_area.value = (
                    "<p><b>Interpretation failed:</b> "
                    f"{escape(str(triage.rejection_reason))}</p>"
                    "<p>The model answered:</p>"
                    f"<pre style='white-space:pre-wrap'>"
                    f"{escape(triage.raw_response)}</pre>"
                )
                log("Interpretation failed &mdash; see the answer below.")
        except Exception:
            log("Error &mdash; see below.")
            report(traceback.format_exc())
        finally:
            understand_button.disabled = False
        return understood

    def _execute_work():
        """Plan from the instruction and hand the plan to the world."""
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
                status = outcome.get("status", "unknown")
                if status == "ok":
                    log("Execution finished: ok")
                else:
                    # Without the phase and the message there is nothing to act
                    # on: the world, the plan and the robot all fail the same
                    # way from the outside.
                    detail = ", ".join(
                        str(outcome[key])
                        for key in ("phase", "error", "step_index")
                        if outcome.get(key) is not None
                    )
                    log(f"Execution finished: {status}"
                        + (f" &mdash; {detail}" if detail else ""))
            elif result.outcome == "clarification":
                log(f"Planner asks: {escape(str(result.payload))}")
            else:
                log(f"Planner failed: {escape(str(result.payload))}")
        except Exception:
            log("Error &mdash; see below.")
            report(traceback.format_exc())
        finally:
            execute_button.disabled = False

    def _understand(_button):
        understand_button.disabled = True
        execute_button.disabled = True
        threading.Thread(target=_understand_work, daemon=True).start()

    def _execute(_button):
        execute_button.disabled = True
        threading.Thread(target=_execute_work, daemon=True).start()

    def _run_whole_chain():
        """Understand the clip and, if it carried a command, plan and execute.

        The stages already report their own progress, so chaining them only
        removes the two clicks between them.
        """
        understand_button.disabled = True
        execute_button.disabled = True

        def work():
            if _understand_work():
                _execute_work()

        threading.Thread(target=work, daemon=True).start()

    _wire_world_button(
        world_button, log, (robot_choice, environment_choice), stop_world_button
    )
    upload.observe(_on_upload, names="value")
    understand_button.on_click(_understand)
    execute_button.on_click(_execute)

    def _play_sample(_button):
        _accept_audio(
            sample_scene_path().read_bytes(),
            "Sample scene",
            "Running the whole chain ...",
        )
        _run_whole_chain()

    sample_button.on_click(_play_sample)

    controls = [upload, sample_button, understand_button, execute_button]
    if recorder is not None:
        recorder.audio.observe(_on_recording, names="value")
        controls.insert(0, recorder)

    display(
        widgets.VBox(
            [
                header,
                robot_choice,
                environment_choice,
                widgets.HBox([world_button, stop_world_button]),
                widgets.HBox(controls),
                auto_recorder,
                status,
                models_status,
                waveform_area,
                table_area,
                instruction_area,
                plan_area,
            ]
        )
    )

    # After display, so the first report has somewhere to appear.
    _preload_models(models_status)

    def _on_auto_recording(audio_bytes):
        _accept_audio(audio_bytes, "Recording", "Running the whole chain ...")
        _run_whole_chain()

    _watch_for_auto_recording(_on_auto_recording)
    # The container exists only once the widgets are on the page.
    display(Javascript(_auto_recorder_script()))
