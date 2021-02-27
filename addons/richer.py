

NAME = "Richer"
VERSION = "0.0.1"
AUTHORS = "Théo Rozier"
REQUIRES = "prompt_toolkit"


def addon_build():

    from typing import cast, Optional, TextIO
    from prompt_toolkit.shortcuts.progress_bar.formatters import Formatter, Label, Text, Percentage, Bar
    from prompt_toolkit.layout.controls import FormattedTextControl, BufferControl
    from prompt_toolkit.layout.containers import Window, HSplit, VSplit, Container
    from prompt_toolkit.shortcuts import ProgressBar, ProgressBarCounter
    from prompt_toolkit.key_binding.key_processor import KeyPressEvent
    from prompt_toolkit.formatted_text import AnyFormattedText
    from prompt_toolkit.layout.dimension import Dimension
    from prompt_toolkit.application import Application
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import AnyDimension
    from prompt_toolkit.document import Document
    from prompt_toolkit.buffer import Buffer
    from prompt_toolkit.layout import Layout
    from queue import Queue, Full, Empty
    from subprocess import Popen, PIPE
    from threading import Thread
    import asyncio

    class RicherAddon:

        def __init__(self, pmc):
            self.pmc = pmc
            self.progress_bar_formatters = [
                Label(),
                Text(" "),
                Bar(sym_a="#", sym_b="#", sym_c="."),
                Text(" ["),
                ByteProgress(),
                Text("] ["),
                Percentage(),
                Text("]"),
            ]

        def load(self):
            self.pmc.add_message("start.run.richer.title", "Minecraft {} • {} • {}")
            self.pmc.add_message("start.run.richer.command_line", "Command line: {}\n")
            self.pmc.mixin("run_game", self.run_game)
            self.pmc.mixin("download_file", self.download_file)

        def build_application(self, container: Container, keys: KeyBindings) -> Application:
            return Application(
                layout=Layout(container),
                key_bindings=keys,
                full_screen=True
            )

        def run_game(self, _old, proc_args: list, proc_cwd: str, options: dict):

            title_text = self.pmc.get_message("start.run.richer.title",
                                              options.get("version", "unknown_version"),
                                              options.get("username", "anonymous"),
                                              options.get("uuid", "uuid"))

            buffer_window = LimitedBufferWindow(100)

            if "args" in options:
                buffer_window.append(self.pmc.get_message("start.run.richer.command_line", " ".join(options["args"])))
                buffer_window.append("\n")

            container = HSplit([
                VSplit([
                    Window(width=2),
                    Window(FormattedTextControl(text=title_text)),
                ], height=1, style="bg:#005fff fg:black"),
                VSplit([
                    Window(width=1),
                    buffer_window,
                    Window(width=1)
                ])
            ])

            keys = KeyBindings()

            @keys.add("c-c")
            def _exit(event: KeyPressEvent):
                event.app.exit()

            application = self.build_application(container, keys)
            process = Popen(proc_args, cwd=proc_cwd, stdout=PIPE, stderr=PIPE, bufsize=1, universal_newlines=True)

            async def _run_process():
                stdout_reader = ThreadedProcessReader(cast(TextIO, process.stdout))
                stderr_reader = ThreadedProcessReader(cast(TextIO, process.stderr))
                while True:
                    code = process.poll()
                    if code is None:
                        buffer_window.append(stdout_reader.poll(), stderr_reader.poll())
                        await asyncio.sleep(0.1)
                    else:
                        break

            async def _run():
                _done, _pending = await asyncio.wait((
                    _run_process(),
                    application.run_async()
                ), return_when=asyncio.FIRST_COMPLETED)
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                if application.is_running:
                    application.exit()

            asyncio.get_event_loop().run_until_complete(_run())

        def download_file(self, old, entry, *args, **kwargs):

            safe_delete(kwargs, "progress_callback")
            safe_delete(kwargs, "end_callback")
            final_issue: Optional[str] = None

            with ProgressBar(formatters=self.progress_bar_formatters) as pb:

                progress_task = pb(label=entry.name, total=entry.size)

                def progress_callback(p_dl_size: int, _p_size: int, _p_dl_total_size: int, _p_total_size: int):
                    progress_task.items_completed = p_dl_size
                    pb.invalidate()

                def end_callback(issue: Optional[str]):
                    nonlocal final_issue
                    final_issue = issue

                ret = old(entry, *args, **kwargs, progress_callback=progress_callback, end_callback=end_callback)

            if final_issue is not None:
                if final_issue.startswith("url_error "):
                    self.pmc.print("url_error.reason", final_issue[len("url_error "):])
                else:
                    self.pmc.print("download.{}".format(final_issue))

            return ret

    class LimitedBufferWindow:

        def __init__(self, limit: int):
            self.buffer = Buffer(read_only=True)
            self.string_buffer = RollingStringBuffer(limit)
            self.window = Window(content=BufferControl(buffer=self.buffer))

        def append(self, *texts: str):
            modified = False
            for text in texts:
                if self.string_buffer.append(text):
                    modified = True
            if modified:
                cursor_pos = None
                new_text = self.string_buffer.get()\
                    .replace("\t", "    ")
                if self.buffer.cursor_position < len(self.buffer.text):
                    cursor_pos = self.buffer.cursor_position
                self.buffer.set_document(Document(text=new_text, cursor_position=cursor_pos), bypass_readonly=True)

        def __pt_container__(self):
            return self.window

    class RollingStringBuffer:

        def __init__(self, limit: int):
            self._strings = []
            self._limit = limit

        def append(self, txt: Optional[str]) -> bool:
            if txt is not None and len(txt):
                self._strings.append(txt)
                while len(self._strings) > self._limit:
                    self._strings.pop(0)
                return True
            else:
                return False

        def get(self) -> str:
            return "".join(self._strings)

    class ThreadedProcessReader:

        def __init__(self, in_stream: TextIO):
            self._input = in_stream
            self._queue = Queue(100)
            self._thread = Thread(target=self._entry, daemon=True)
            self._thread.start()

        def _entry(self):
            for line in iter(self._input.readline, b''):
                try:
                    self._queue.put_nowait(line)
                except Full:
                    pass
            self._input.close()

        def poll(self) -> Optional[str]:
            try:
                return self._queue.get_nowait()
            except Empty:
                return None

    class ByteProgress(Formatter):

        template = "<current>{current}</current>"

        def format(self, progress_bar: "ProgressBar", progress: "ProgressBarCounter[object]", width: int) -> AnyFormattedText:
            n = progress.items_completed
            if n < 1000:
                return "{:4.0f}B".format(n)
            elif n < 1000000:
                return "{:4.0f}kB".format(n // 1000)
            elif n < 1000000000:
                return "{:4.0f}MB".format(n // 1000000)
            else:
                return "{:4.0f}GB".format(n // 1000000000)

        def get_width(self, progress_bar: "ProgressBar") -> AnyDimension:
            width = 5
            for counter in progress_bar.counters:
                if counter.items_completed >= 1000:
                    width = 6
                    break
            return Dimension.exact(width)

    def safe_delete(owner: dict, name: str):
        if name in owner:
            del owner[name]

    return RicherAddon
