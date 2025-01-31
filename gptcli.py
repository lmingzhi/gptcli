#!/usr/bin/env python3

import os
import json
import inspect
import argparse
from argparse import Namespace
from typing import List

from rich.console import Console
from rich.markdown import Markdown
from rich.live import Live

import cmd2
from cmd2 import argparse_custom, with_argparser, Settable

import openai

class Config:
    sep = Markdown("---")
    baseDir = os.path.dirname(os.path.realpath(__file__))
    default = os.path.join(baseDir, "config.json")
    mdSep = '\n\n' + '-' * 10 + '\n'
    encodings = ["utf8", "gbk"]

    def __init__(self, file=None) -> None:
        self.cfg = {}
        if file:
            self.load(file)

    def load(self, file):
        with open(file, "r") as f:
            self.cfg = json.load(f)
        c = self.cfg
        self.api_key = c.get("api_key", c.get("key", openai.api_key)) # compatible with history key
        self.api_base = c.get("api_base", openai.api_base)
        self.model = c.get("model", "gpt-3.5-turbo")
        self.prompt = c.get("prompt", [])
        self.stream = c.get("stream", False)
        self.stream_render = c.get("stream_render", False)
        self.response = c.get("response", False)
        self.proxy = c.get("proxy", "")
        self.showtokens = c.get("showtokens", False)

        # azure
        # =========================================================
        self.api_type = c.get("api_type", "")
        self.api_version = c.get("api_version", "")        
        # =========================================================

    def get(self, key, default=None):
        return self.cfg.get(key, default)


class GptCli(cmd2.Cmd):
    prompt = "gptcli> "

    def __init__(self, config):
        super().__init__(
            allow_cli_args=False,
            allow_redirection=False,
            shortcuts={},
        )
        self.aliases[".exit"] = ".quit"
        self.doc_header = "gptcli commands (use '.help -v' for verbose/'.help <topic>' for details):"
        self.hidden_commands = [
            "._relative_run_script", ".run_script", ".run_pyscript",
            ".eof", ".history", ".macro", ".shell", ".shortcuts", ".alias"]
        for sk in ["allow_style", "always_show_hint", "echo", "feedback_to_output",
                  "max_completion_items", "quiet", "timing"]:
            self.remove_settable(sk)
        self.console = Console()
        self.session = []
        # Init config
        self.config = Config(config)
        openai.api_key = self.config.api_key
        if self.config.api_base:
            openai.api_base = self.config.api_base
        if self.config.proxy:
            self.print("Proxy:", self.config.proxy)
            openai.proxy = self.config.proxy


        # azure
        # =========================================================
        # openai.api_type = "azure"
        # openai.api_base = os.getenv("AZURE_OPENAI_ENDPOINT") 
        # openai.api_version = "2023-05-15"
        # openai.api_key = os.getenv("AZURE_OPENAI_KEY")
        if self.config.api_type:
            self.print("api_type:", self.config.api_type)
            openai.api_type = self.config.api_type

        if self.config.api_version:
            self.print("api_version:", self.config.api_version)
            openai.api_version = self.config.api_version
        # =========================================================


        self.print("Response in prompt:", self.config.response)
        self.print("Stream mode:", self.config.stream)
        # Init settable
        # NOTE: proxy is not settable in runtime since openai use pre-configured session
        self.add_settable(Settable("api_key", str, "OPENAI_API_KEY", self.config, onchange_cb=self.openai_set))
        self.add_settable(Settable("api_base", str, "OPENAI_API_BASE", self.config, onchange_cb=self.openai_set))
        self.add_settable(Settable("response", bool, "Attach response in prompt", self.config))
        self.add_settable(Settable("stream", bool, "Enable stream mode", self.config))
        self.add_settable(Settable("stream_render", bool, "Render live markdown in stream mode", self.config))
        self.add_settable(Settable("model", str, "OPENAI model", self.config))
        self.add_settable(Settable("showtokens", bool, "Show tokens used with the output", self.config))
        # MISC
        with self.console.capture() as capture:
            self.print(f"[bold yellow]{self.prompt}[/]", end="")
        self.prompt = capture.get()

        self.single_tokens_used = 0
        self.total_tokens_used  = 0

    def openai_set(self, param, old, new):
        self.print(f"openai.{param} = {new}")
        setattr(openai, param, new)

    def cmd_func(self, command: str):
        if command.startswith("."):
            command = command[1:]
            return super().cmd_func(command)
        if inspect.currentframe().f_back.f_code.co_name == "_register_subcommands":
            return super().cmd_func(command)
        return None

    def get_all_commands(self) -> List[str]:
        return list(map(lambda c: f".{c}", super().get_all_commands()))

    def default(self, statement: cmd2.Statement):
        self.handle_input(statement.raw)

    def print(self, *msg, **kwargs):
        self.console.print(*msg, **kwargs)

    def handle_input(self, content: str):
        if not content:
            return
        self.session.append({"role": "user", "content": content})

        # azure
        # =========================================================
        if self.config.api_type == 'azure':
            if self.config.stream:
                answer = self.query_azure_stream(self.messages)
            else:
                answer = self.query_azure(self.messages)
        else:
            if self.config.stream:
                answer = self.query_openai_stream(self.messages)
            else:
                answer = self.query_openai(self.messages)
        # =========================================================

        if not answer:
            self.session.pop()
        else:
            self.session.append({"role": "assistant", "content": answer})

        if self.config.showtokens:
            self.console.log(f"Tokens Used: {self.single_tokens_used}/{self.total_tokens_used}")

    @property
    def messages(self):
        msgs = []
        msgs.extend(self.config.prompt)
        if self.config.response:
            msgs.extend(self.session)
        else:
            msgs.extend([s for s in self.session if s["role"] != "assistant"])
        return msgs

    def load_session(self, file, mode="md", encoding=None, append=False):
        if not append:
            self.session.clear()
        with open(file, "r", encoding=encoding) as f:
            data = f.read()
        if mode == "json":
            self.session.extend(json.loads(data))
        elif mode == "md":
            for chat in data.split(Config.mdSep):
                role, content = chat.split(": ", 1)
                self.session.append({"role": role, "content": content})
        self.print("Load {} records from {}".format(len(self.session), file))

    def save_session(self, file, mode="md", encoding=None):
        self.print("Save {} records to {}".format(len(self.session), file))
        if mode == "json":
            data = json.dumps(self.session, indent=2)
        elif mode == "md":
            chats = ["{}: {}".format(chat["role"], chat["content"])
                     for chat in self.session]
            data = Config.mdSep.join(chats)
        with open(file, "w", encoding=encoding) as f:
            f.write(data)
    
    # Reference:
    # https://platform.openai.com/docs/guides/chat/managing-tokens
    def num_tokens_from_messages(self, messages):
        """Returns the number of tokens used by a list of messages."""
        import tiktoken
        model = self.config.model
        try:
            encoding = tiktoken.encoding_for_model(model)
        except KeyError:
            encoding = tiktoken.get_encoding("cl100k_base")


        # azure
        # =========================================================
        try:
            num_tokens = 0
            for message in messages:
                num_tokens += 4  # every message follows <im_start>{role/name}\n{content}<im_end>\n
                for key, value in message.items():
                    num_tokens += len(encoding.encode(value))
                    if key == "name":  # if there's a name, the role is omitted
                        num_tokens += -1  # role is always required and always 1 token
            num_tokens += 2  # every reply is primed with <im_start>assistant
            return num_tokens
        except Exception as e:
            # return 0
            raise NotImplementedError(f"""num_tokens_from_messages() is not presently implemented for model {model}.
        See https://github.com/openai/openai-python/blob/main/chatml.md for information on how messages are converted to tokens.""")
        # =========================================================

        # if model == "gpt-3.5-turbo":  # note: future models may deviate from this
        #     num_tokens = 0
        #     for message in messages:
        #         num_tokens += 4  # every message follows <im_start>{role/name}\n{content}<im_end>\n
        #         for key, value in message.items():
        #             num_tokens += len(encoding.encode(value))
        #             if key == "name":  # if there's a name, the role is omitted
        #                 num_tokens += -1  # role is always required and always 1 token
        #     num_tokens += 2  # every reply is primed with <im_start>assistant
        #     return num_tokens
        # else:
        #     raise NotImplementedError(f"""num_tokens_from_messages() is not presently implemented for model {model}.
        # See https://github.com/openai/openai-python/blob/main/chatml.md for information on how messages are converted to tokens.""")

        # =========================================================

    def query_openai(self, messages) -> str:
        try:
            response = openai.ChatCompletion.create(
                model=self.config.model,
                messages=messages
            )
            content = response["choices"][0]["message"]["content"]
            self.print(Markdown(content), Config.sep)

            self.single_tokens_used = response["usage"]["total_tokens"]
            self.total_tokens_used += self.single_tokens_used
            return content
        except openai.error.OpenAIError as e:
            self.print("OpenAIError:", e)
        return ""

    def query_openai_stream(self, messages) -> str:
        answer = ""
        try:
            response = openai.ChatCompletion.create(
                model=self.config.model,
                messages=messages,
                stream=True)
            with Live(auto_refresh=False, vertical_overflow="visible") as lv:
                for part in response:
                    finish_reason = part["choices"][0]["finish_reason"]
                    if "content" in part["choices"][0]["delta"]:
                        content = part["choices"][0]["delta"]["content"]
                        answer += content
                        if self.config.stream_render:
                            lv.update(Markdown(answer), refresh=True)
                        else:
                            lv.update(answer, refresh=True)
                    elif finish_reason:
                        if answer:
                            lv.update(Markdown(answer), refresh=True)

        except KeyboardInterrupt:
            self.print("Canceled")
        except openai.error.OpenAIError as e:
            self.print("OpenAIError:", e)
            answer = ""
        self.print(Config.sep)
        self.single_tokens_used = self.num_tokens_from_messages(messages)
        self.total_tokens_used += self.single_tokens_used
        return answer



    # azure
    # ============================================================
    def query_azure(self, messages) -> str:
        try:
            response = openai.ChatCompletion.create(
                engine=self.config.model,
                messages=messages
            )
            content = response["choices"][0]["message"]["content"]
            self.print(Markdown(content), Config.sep)

            self.single_tokens_used = response["usage"]["total_tokens"]
            self.total_tokens_used += self.single_tokens_used
            return content
        except openai.error.OpenAIError as e:
            self.print("OpenAIError:", e)
        return ""

    def query_azure_stream(self, messages) -> str:
        answer = ""
        try:
            response = openai.ChatCompletion.create(
                engine=self.config.model,
                messages=messages,
                stream=True)
            with Live(auto_refresh=False, vertical_overflow="visible") as lv:
                for part in response:
                    finish_reason = part["choices"][0]["finish_reason"]
                    if "content" in part["choices"][0]["delta"]:
                        content = part["choices"][0]["delta"]["content"]
                        answer += content
                        if self.config.stream_render:
                            lv.update(Markdown(answer), refresh=True)
                        else:
                            lv.update(answer, refresh=True)
                    elif finish_reason:
                        if answer:
                            lv.update(Markdown(answer), refresh=True)

        except KeyboardInterrupt:
            self.print("Canceled")
        except openai.error.OpenAIError as e:
            self.print("OpenAIError:", e)
            answer = ""
        self.print(Config.sep)
        self.single_tokens_used = self.num_tokens_from_messages(messages)
        self.total_tokens_used += self.single_tokens_used
        return answer
    # ============================================================

    parser_ml = argparse_custom.DEFAULT_ARGUMENT_PARSER()
    @with_argparser(parser_ml)
    def do_multiline(self, args):
        "input multiple lines, end with ctrl-d(Linux/macOS) or ctrl-z(Windows). Cancel with ctrl-c"
        contents = []
        while True:
            try:
                line = input("> ")
            except EOFError:
                self.print("--- EOF ---")
                break
            except KeyboardInterrupt:
                self.print("^C")
                return
            contents.append(line)
        self.handle_input("\n".join(contents))

    parser_reset = argparse_custom.DEFAULT_ARGUMENT_PARSER()
    @with_argparser(parser_reset)
    def do_reset(self, args):
        "Reset session, i.e. clear chat history"
        self.session.clear()
        self.print("session cleared.")

    parser_save = argparse_custom.DEFAULT_ARGUMENT_PARSER()
    parser_save.add_argument("-m", dest="mode", choices=["json", "md"],
                             default="md", help="save as json or markdown (default: md)")
    parser_save.add_argument("-e", dest="encoding", choices=Config.encodings,
                             default=Config.encodings[0], help="file encoding")
    parser_save.add_argument("file", help="target file to save",
                            completer=cmd2.Cmd.path_complete)
    @with_argparser(parser_save)
    def do_save(self, args: Namespace):
        "Save current conversation to Markdown/JSON file"
        self.save_session(args.file, args.mode, args.encoding)

    parser_load = argparse_custom.DEFAULT_ARGUMENT_PARSER()
    parser_load.add_argument("-a", dest="append", action="store_true",
                             help="append to current chat, by default current chat will be cleared")
    parser_load.add_argument("-m", dest="mode", choices=["json", "md"],
                             default="md", help="load as json or markdown (default: md)")
    parser_load.add_argument("-e", dest="encoding", choices=Config.encodings,
                             default=Config.encodings[0], help="file encoding")
    parser_load.add_argument("file", help="target file to load",
                            completer=cmd2.Cmd.path_complete)
    @with_argparser(parser_load)
    def do_load(self, args: Namespace):
        "Load conversation from Markdown/JSON file"
        self.load_session(args.file, args.mode, args.encoding, args.append)

    def do_tokens(self, args: Namespace):
        "Display total tokens used this session"
        self.print(f"Total tokens used this session: {self.total_tokens_used}")

def main():
    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("-c", dest="config", help="path to config.json", default=Config.default)
    args = parser.parse_args()

    app = GptCli(args.config)
    app.cmdloop()

if __name__ == '__main__':
    main()
