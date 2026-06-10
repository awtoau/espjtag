from __future__ import annotations

import ast
import fnmatch
import operator
import re
from pathlib import Path
from typing import Callable, Any


class TclError(Exception):
    pass


class ReturnSignal(Exception):
    def __init__(self, value: str):
        self.value = value


class BreakSignal(Exception):
    pass


class ContinueSignal(Exception):
    pass


class MiniJimTcl:
    def __init__(self):
        self.vars: dict[str, str] = {}
        self.procs: dict[str, tuple[list[str], str]] = {}
        self.commands: dict[str, Callable[[list[str]], str]] = {}

        self.register_core_commands()

    def eval(self, script: str) -> str:
        result = ""

        for command in self.split_commands(script):
            words = self.parse_words(command)
            if not words:
                continue

            words = [self.substitute_word(w) for w in words]
            result = self.dispatch(words)

        return result

    def register(self, name: str, fn: Callable[[list[str]], str]) -> None:
        self.commands[name] = fn

    def split_commands(self, script: str) -> list[str]:
        commands = []
        current = []
        brace_depth = 0
        bracket_depth = 0
        in_quote = False
        escaped = False
        comment_possible = True
        in_comment = False

        for ch in script:
            if in_comment:
                if ch == "\n":
                    in_comment = False
                    if current:
                        commands.append("".join(current).strip())
                        current = []
                    comment_possible = True
                continue

            if escaped:
                current.append(ch)
                escaped = False
                comment_possible = False
                continue

            if ch == "\\":
                current.append(ch)
                escaped = True
                continue

            if comment_possible and ch == "#":
                in_comment = True
                continue

            if ch == '"' and brace_depth == 0:
                in_quote = not in_quote
                current.append(ch)
                comment_possible = False
                continue

            if not in_quote:
                if ch == "{" and bracket_depth == 0:
                    brace_depth += 1
                elif ch == "}" and bracket_depth == 0:
                    brace_depth -= 1
                    if brace_depth < 0:
                        raise TclError("unmatched }")
                elif ch == "[" and brace_depth == 0:
                    bracket_depth += 1
                elif ch == "]" and brace_depth == 0:
                    bracket_depth -= 1
                    if bracket_depth < 0:
                        raise TclError("unmatched ]")

                if ch in ";\n" and brace_depth == 0 and bracket_depth == 0:
                    cmd = "".join(current).strip()
                    if cmd:
                        commands.append(cmd)
                    current = []
                    comment_possible = True
                    continue

            current.append(ch)
            if not ch.isspace():
                comment_possible = False

        if brace_depth != 0:
            raise TclError("unmatched brace")
        if bracket_depth != 0:
            raise TclError("unmatched bracket")
        if in_quote:
            raise TclError("unmatched quote")

        cmd = "".join(current).strip()
        if cmd:
            commands.append(cmd)

        return commands

    def parse_words(self, command: str) -> list[str]:
        words = []
        i = 0
        n = len(command)

        while i < n:
            while i < n and command[i].isspace():
                i += 1

            if i >= n:
                break

            if command[i] == "{":
                word, i = self.read_braced(command, i)
                words.append("{" + word + "}")
            elif command[i] == '"':
                word, i = self.read_quoted(command, i)
                words.append('"' + word + '"')
            else:
                word, i = self.read_bare(command, i)
                words.append(word)

        return words

    def read_braced(self, s: str, i: int) -> tuple[str, int]:
        i += 1
        start = i
        depth = 1
        escaped = False

        while i < len(s):
            ch = s[i]

            if escaped:
                escaped = False
                i += 1
                continue

            if ch == "\\":
                escaped = True
                i += 1
                continue

            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return s[start:i], i + 1

            i += 1

        raise TclError("unmatched {")

    def read_quoted(self, s: str, i: int) -> tuple[str, int]:
        i += 1
        out = []
        escaped = False
        bracket_depth = 0

        while i < len(s):
            ch = s[i]

            if escaped:
                out.append("\\" + ch)
                escaped = False
                i += 1
                continue

            if ch == "\\":
                escaped = True
                i += 1
                continue

            if ch == "[":
                bracket_depth += 1
            elif ch == "]":
                bracket_depth -= 1
                if bracket_depth < 0:
                    raise TclError("unmatched ] in quoted string")

            if ch == '"' and bracket_depth == 0:
                return "".join(out), i + 1

            out.append(ch)
            i += 1

        raise TclError("unmatched quote")

    def read_bare(self, s: str, i: int) -> tuple[str, int]:
        out = []
        escaped = False
        brace_depth = 0
        bracket_depth = 0

        while i < len(s):
            ch = s[i]

            if escaped:
                out.append("\\" + ch)
                escaped = False
                i += 1
                continue

            if ch == "\\":
                escaped = True
                i += 1
                continue

            if ch == "[":
                bracket_depth += 1
            elif ch == "]":
                bracket_depth -= 1
                if bracket_depth < 0:
                    raise TclError("unmatched ]")

            if bracket_depth == 0:
                if ch == "{":
                    brace_depth += 1
                elif ch == "}":
                    brace_depth -= 1

            if ch.isspace() and brace_depth == 0 and bracket_depth == 0:
                break

            out.append(ch)
            i += 1

        return "".join(out), i

    def substitute_word(self, word: str) -> str:
        if len(word) >= 2 and word[0] == "{" and word[-1] == "}":
            return word[1:-1]

        if len(word) >= 2 and word[0] == '"' and word[-1] == '"':
            word = word[1:-1]

        return self.substitute(word)

    def substitute(self, s: str) -> str:
        out = []
        i = 0

        while i < len(s):
            ch = s[i]

            if ch == "\\":
                if i + 1 < len(s):
                    out.append(self.backslash_subst(s[i + 1]))
                    i += 2
                else:
                    i += 1

            elif ch == "$":
                name, i2 = self.read_var_name(s, i + 1)
                if not name:
                    out.append("$")
                    i += 1
                else:
                    out.append(self.vars.get(name, ""))
                    i = i2

            elif ch == "[":
                body, i2 = self.read_command_subst(s, i)
                out.append(self.eval(body))
                i = i2

            else:
                out.append(ch)
                i += 1

        return "".join(out)

    def read_var_name(self, s: str, i: int) -> tuple[str, int]:
        if i < len(s) and s[i] == "{":
            j = s.find("}", i + 1)
            if j < 0:
                raise TclError("unmatched ${")
            return s[i + 1:j], j + 1

        start = i
        while i < len(s) and (s[i].isalnum() or s[i] in "_:."):
            i += 1

        return s[start:i], i

    def read_command_subst(self, s: str, i: int) -> tuple[str, int]:
        i += 1
        start = i
        depth = 1
        in_quote = False
        brace_depth = 0
        escaped = False

        while i < len(s):
            ch = s[i]

            if escaped:
                escaped = False
                i += 1
                continue

            if ch == "\\":
                escaped = True
                i += 1
                continue

            if ch == '"' and brace_depth == 0:
                in_quote = not in_quote
                i += 1
                continue

            if not in_quote:
                if ch == "{":
                    brace_depth += 1
                elif ch == "}":
                    brace_depth -= 1
                elif ch == "[" and brace_depth == 0:
                    depth += 1
                elif ch == "]" and brace_depth == 0:
                    depth -= 1
                    if depth == 0:
                        return s[start:i], i + 1

            i += 1

        raise TclError("unmatched [")

    def backslash_subst(self, ch: str) -> str:
        return {
            "n": "\n",
            "t": "\t",
            "r": "\r",
            "\\": "\\",
            '"': '"',
            "{": "{",
            "}": "}",
            "[": "[",
            "]": "]",
            "$": "$",
        }.get(ch, ch)

    def dispatch(self, words: list[str]) -> str:
        name = words[0]
        args = words[1:]

        if name in self.commands:
            return self.commands[name](args)

        if name in self.procs:
            return self.call_proc(name, args)

        raise TclError(f"unknown command: {name}")

    def call_proc(self, name: str, args: list[str]) -> str:
        params, body = self.procs[name]

        if len(args) != len(params):
            raise TclError(f"{name}: wrong # args")

        old_vars = self.vars.copy()
        try:
            for p, a in zip(params, args):
                self.vars[p] = a
            try:
                return self.eval(body)
            except ReturnSignal as r:
                return r.value
        finally:
            self.vars = old_vars

    def register_core_commands(self) -> None:
        self.register("set", self.cmd_set)
        self.register("puts", self.cmd_puts)
        self.register("expr", self.cmd_expr)
        self.register("if", self.cmd_if)
        self.register("foreach", self.cmd_foreach)
        self.register("proc", self.cmd_proc)
        self.register("return", self.cmd_return)
        self.register("source", self.cmd_source)
        self.register("list", self.cmd_list)
        self.register("llength", self.cmd_llength)
        self.register("lindex", self.cmd_lindex)
        # added for OpenOCD configs (Jim Tcl semantics)
        self.register("string", self.cmd_string)
        self.register("while", self.cmd_while)
        self.register("for", self.cmd_for)
        self.register("incr", self.cmd_incr)
        self.register("catch", self.cmd_catch)
        self.register("global", self.cmd_global)
        self.register("append", self.cmd_append)
        self.register("lappend", self.cmd_lappend)
        self.register("info", self.cmd_info)
        self.register("unset", self.cmd_unset)
        self.register("break", lambda a: (_ for _ in ()).throw(BreakSignal()))
        self.register("continue", lambda a: (_ for _ in ()).throw(ContinueSignal()))

    def cmd_set(self, args: list[str]) -> str:
        if len(args) == 1:
            return self.vars.get(args[0], "")
        if len(args) == 2:
            self.vars[args[0]] = args[1]
            return args[1]
        raise TclError("set: wrong # args")

    def cmd_puts(self, args: list[str]) -> str:
        if len(args) != 1:
            raise TclError("puts: wrong # args")
        print(args[0])
        return ""

    def cmd_expr(self, args: list[str]) -> str:
        if len(args) != 1:
            raise TclError("expr: wrong # args")
        return str(self.safe_expr(args[0]))

    def cmd_if(self, args: list[str]) -> str:
        if len(args) < 2:
            raise TclError("if: wrong # args")

        cond = args[0]
        then_body = args[1]

        if self.truthy(str(self.safe_expr(cond))):
            return self.eval(then_body)

        i = 2
        while i < len(args):
            if args[i] == "elseif":
                if i + 2 >= len(args):
                    raise TclError("if: bad elseif")
                if self.truthy(str(self.safe_expr(args[i + 1]))):
                    return self.eval(args[i + 2])
                i += 3
            elif args[i] == "else":
                if i + 1 >= len(args):
                    raise TclError("if: bad else")
                return self.eval(args[i + 1])
            else:
                raise TclError("if: unexpected token")

        return ""

    def cmd_foreach(self, args: list[str]) -> str:
        if len(args) != 3:
            raise TclError("foreach: expected var list body")

        var_name, items, body = args
        result = ""

        for item in self.tcl_list_split(items):
            self.vars[var_name] = item
            result = self.eval(body)

        return result

    def cmd_proc(self, args: list[str]) -> str:
        if len(args) != 3:
            raise TclError("proc: expected name args body")

        name, params, body = args
        self.procs[name] = (self.tcl_list_split(params), body)
        return ""

    def cmd_return(self, args: list[str]) -> str:
        value = args[0] if args else ""
        raise ReturnSignal(value)

    def cmd_source(self, args: list[str]) -> str:
        if len(args) != 1:
            raise TclError("source: wrong # args")

        path = Path(args[0])
        return self.eval(path.read_text())

    def cmd_list(self, args: list[str]) -> str:
        return " ".join(self.tcl_list_quote(a) for a in args)

    def cmd_llength(self, args: list[str]) -> str:
        if len(args) != 1:
            raise TclError("llength: wrong # args")
        return str(len(self.tcl_list_split(args[0])))

    def cmd_lindex(self, args: list[str]) -> str:
        if len(args) != 2:
            raise TclError("lindex: wrong # args")

        items = self.tcl_list_split(args[0])
        idx = int(args[1])

        if idx < 0 or idx >= len(items):
            return ""

        return items[idx]

    def cmd_string(self, args: list[str]) -> str:
        if not args:
            raise TclError("string: wrong # args")
        sub, rest = args[0], args[1:]
        if sub == "compare":
            a, b = rest[0], rest[1]
            return str((a > b) - (a < b))            # -1 / 0 / 1
        if sub == "equal":
            return "1" if rest[0] == rest[1] else "0"
        if sub == "length":
            return str(len(rest[0]))
        if sub == "index":
            s, i = rest[0], int(rest[1])
            return s[i] if 0 <= i < len(s) else ""
        if sub == "range":
            s = rest[0]
            a = 0 if rest[1] == "start" else int(rest[1])
            b = len(s) - 1 if rest[2] == "end" else int(rest[2])
            return s[a:b + 1]
        if sub == "match":
            return "1" if fnmatch.fnmatchcase(rest[1], rest[0]) else "0"
        if sub == "first":
            idx = rest[1].find(rest[0])
            return str(idx)
        if sub == "tolower":
            return rest[0].lower()
        if sub == "toupper":
            return rest[0].upper()
        if sub == "trim":
            return rest[0].strip()
        raise TclError(f"string: unsupported subcommand {sub!r}")

    def cmd_while(self, args: list[str]) -> str:
        if len(args) != 2:
            raise TclError("while: wrong # args")
        cond, body = args
        result = ""
        while self.truthy(str(self.safe_expr(cond))):
            try:
                result = self.eval(body)
            except BreakSignal:
                break
            except ContinueSignal:
                continue
        return result

    def cmd_for(self, args: list[str]) -> str:
        if len(args) != 4:
            raise TclError("for: expected start test next body")
        start, test, nxt, body = args
        self.eval(start)
        result = ""
        while self.truthy(str(self.safe_expr(test))):
            try:
                result = self.eval(body)
            except BreakSignal:
                break
            except ContinueSignal:
                pass
            self.eval(nxt)
        return result

    def cmd_incr(self, args: list[str]) -> str:
        name = args[0]
        amount = int(args[1]) if len(args) > 1 else 1
        val = int(self.vars.get(name, "0"), 0) + amount
        self.vars[name] = str(val)
        return str(val)

    def cmd_catch(self, args: list[str]) -> str:
        if not args:
            raise TclError("catch: wrong # args")
        try:
            r, code = self.eval(args[0]), "0"
        except ReturnSignal as rs:
            r, code = rs.value, "0"
        except (BreakSignal, ContinueSignal):
            raise
        except Exception as e:                       # noqa: BLE001 — Tcl catch
            r, code = str(e), "1"
        if len(args) > 1:
            self.vars[args[1]] = r
        return code

    def cmd_global(self, args: list[str]) -> str:
        # This mini interp copies all vars into a proc frame, so globals are
        # already visible for READ. Write-back to the global frame isn't modeled.
        return ""

    def cmd_append(self, args: list[str]) -> str:
        name = args[0]
        self.vars[name] = self.vars.get(name, "") + "".join(args[1:])
        return self.vars[name]

    def cmd_lappend(self, args: list[str]) -> str:
        name = args[0]
        cur = self.vars.get(name, "")
        items = self.tcl_list_split(cur) if cur else []
        items += args[1:]
        self.vars[name] = " ".join(self.tcl_list_quote(x) for x in items)
        return self.vars[name]

    def cmd_info(self, args: list[str]) -> str:
        if args and args[0] == "exists":
            return "1" if args[1] in self.vars else "0"
        if args and args[0] == "commands":
            return " ".join(list(self.commands) + list(self.procs))
        return ""

    def cmd_unset(self, args: list[str]) -> str:
        for a in args:
            self.vars.pop(a, None)
        return ""

    def truthy(self, value: str) -> bool:
        return value not in ("", "0", "False", "false")

    def tcl_list_split(self, s: str) -> list[str]:
        return [self.substitute_word(w) for w in self.parse_words(s)]

    def tcl_list_quote(self, s: str) -> str:
        if not s or any(c.isspace() or c in "{}[]$;\\" for c in s):
            return "{" + s.replace("\\", "\\\\").replace("}", "\\}") + "}"
        return s

    def safe_expr(self, expression: str) -> Any:
        # Jim's expr performs $var and [cmd] substitution INSIDE the expression,
        # then evaluates it. Do that first, then translate the C-style boolean
        # operators Python's parser rejects (&& || !) before handing to ast.
        expr = self.substitute(expression.strip())
        expr = expr.replace("&&", " and ").replace("||", " or ")
        expr = re.sub(r"!(?!=)", " not ", expr)          # logical not, not !=
        tree = ast.parse(expr.strip(), mode="eval")
        return self.eval_ast(tree.body)

    def eval_ast(self, node: ast.AST) -> Any:
        bin_ops = {
            ast.Add: operator.add,
            ast.Sub: operator.sub,
            ast.Mult: operator.mul,
            ast.Div: operator.truediv,
            ast.FloorDiv: operator.floordiv,
            ast.Mod: operator.mod,
            ast.Pow: operator.pow,
            ast.LShift: operator.lshift,
            ast.RShift: operator.rshift,
            ast.BitAnd: operator.and_,
            ast.BitOr: operator.or_,
            ast.BitXor: operator.xor,
        }

        cmp_ops = {
            ast.Eq: operator.eq,
            ast.NotEq: operator.ne,
            ast.Lt: operator.lt,
            ast.LtE: operator.le,
            ast.Gt: operator.gt,
            ast.GtE: operator.ge,
        }

        unary_ops = {
            ast.UAdd: operator.pos,
            ast.USub: operator.neg,
            ast.Not: operator.not_,
            ast.Invert: operator.invert,
        }

        bool_ops = {
            ast.And: all,
            ast.Or: any,
        }

        if isinstance(node, ast.Constant):
            return node.value

        if isinstance(node, ast.BinOp):
            op = bin_ops.get(type(node.op))
            if not op:
                raise TclError("expr: unsupported operator")
            return op(self.eval_ast(node.left), self.eval_ast(node.right))

        if isinstance(node, ast.UnaryOp):
            op = unary_ops.get(type(node.op))
            if not op:
                raise TclError("expr: unsupported unary operator")
            return op(self.eval_ast(node.operand))

        if isinstance(node, ast.Compare):
            left = self.eval_ast(node.left)
            for op_node, comp_node in zip(node.ops, node.comparators):
                op = cmp_ops.get(type(op_node))
                if not op:
                    raise TclError("expr: unsupported comparison")
                right = self.eval_ast(comp_node)
                if not op(left, right):
                    return False
                left = right
            return True

        if isinstance(node, ast.BoolOp):
            op = bool_ops.get(type(node.op))
            if not op:
                raise TclError("expr: unsupported bool op")
            return op(self.eval_ast(v) for v in node.values)

        raise TclError(f"expr: unsupported expression node {type(node).__name__}")
