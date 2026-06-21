"""简单的控制台输出和日志模块"""

from datetime import datetime


class Colors:
    """ANSI颜色代码"""

    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    WHITE = "\033[37m"
    BOLD = "\033[1m"
    UNDERLINE = "\033[4m"
    RESET = "\033[0m"


class Console:
    """简单的控制台输出类"""

    def __init__(self, enable_colors: bool = True):
        self.enable_colors = enable_colors

    def _colorize(self, text: str, color: str) -> str:
        """给文本添加颜色"""
        if not self.enable_colors:
            return text
        return f"{color}{text}{Colors.RESET}"

    def print(self, text: str, color: str | None = None) -> None:
        """打印文本"""
        if color:
            text = self._colorize(text, color)
        print(text)

    def success(self, text: str) -> None:
        """打印成功信息"""
        self.print(f"✓ {text}", Colors.GREEN)

    def error(self, text: str) -> None:
        """打印错误信息"""
        self.print(f"✗ {text}", Colors.RED)

    def warning(self, text: str) -> None:
        """打印警告信息"""
        self.print(f"⚠ {text}", Colors.YELLOW)

    def info(self, text: str) -> None:
        """打印信息"""
        self.print(f"ℹ {text}", Colors.BLUE)

    def debug(self, text: str) -> None:
        """打印调试信息"""
        self.print(f"→ {text}", Colors.CYAN)

    def header(self, text: str) -> None:
        """打印标题"""
        self.print(f"\n{text}", Colors.BOLD + Colors.BLUE)
        self.print("=" * len(text), Colors.BLUE)


class Logger:
    """简单的日志类"""

    def __init__(self, level: str = "INFO", enable_colors: bool = True):
        self.level = level.upper()
        self.console = Console(enable_colors)
        self.levels = {"DEBUG": 0, "INFO": 1, "WARNING": 2, "ERROR": 3, "CRITICAL": 4}

    def _should_log(self, level: str) -> bool:
        """检查是否应该记录日志"""
        return self.levels.get(level.upper(), 0) >= self.levels.get(self.level, 1)

    def _format_message(self, level: str, message: str) -> str:
        """格式化日志消息"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        return f"[{timestamp}] {level:<8} {message}"

    def debug(self, message: str) -> None:
        """记录调试信息"""
        if self._should_log("DEBUG"):
            formatted = self._format_message("DEBUG", message)
            self.console.print(formatted, Colors.CYAN)

    def info(self, message: str) -> None:
        """记录信息"""
        if self._should_log("INFO"):
            formatted = self._format_message("INFO", message)
            self.console.print(formatted, Colors.BLUE)

    def warning(self, message: str) -> None:
        """记录警告"""
        if self._should_log("WARNING"):
            formatted = self._format_message("WARNING", message)
            self.console.print(formatted, Colors.YELLOW)

    def error(self, message: str) -> None:
        """记录错误"""
        if self._should_log("ERROR"):
            formatted = self._format_message("ERROR", message)
            self.console.print(formatted, Colors.RED)

    def critical(self, message: str) -> None:
        """记录严重错误"""
        if self._should_log("CRITICAL"):
            formatted = self._format_message("CRITICAL", message)
            self.console.print(formatted, Colors.RED + Colors.BOLD)

    def success(self, message: str) -> None:
        """记录成功信息"""
        formatted = self._format_message("SUCCESS", message)
        self.console.print(formatted, Colors.GREEN)


# 全局实例
console = Console()
logger = Logger()


class ProgressBar:
    """简单的进度条"""

    def __init__(self, total: int, width: int = 50, desc: str = ""):
        self.total = total
        self.width = width
        self.desc = desc
        self.current = 0

    def update(self, n: int = 1) -> None:
        """更新进度"""
        self.current += n
        self.display()

    def display(self) -> None:
        """显示进度条"""
        if self.total == 0:
            return

        percent = self.current / self.total
        filled_width = int(self.width * percent)
        bar = "█" * filled_width + "░" * (self.width - filled_width)

        percent_str = f"{percent * 100:.1f}%"
        status = f"\r{self.desc} |{bar}| {self.current}/{self.total} [{percent_str}]"

        print(status, end="", flush=True)

        if self.current >= self.total:
            print()  # 换行

    def finish(self) -> None:
        """完成进度条"""
        self.current = self.total
        self.display()


def status_context(message: str):
    """状态上下文管理器"""

    class StatusContext:
        def __init__(self, msg: str):
            self.message = msg

        def __enter__(self):
            console.info(f"{self.message}...")
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            if exc_type is None:
                console.success(f"{self.message} completed")
            else:
                console.error(f"{self.message} failed: {exc_val}")

    return StatusContext(message)
