from dataclasses import dataclass, field
from app.schemas.workspace import TemplateInfo


@dataclass(frozen=True)
class WorkspaceTemplate:
    id: str
    name: str
    description: str
    category: str
    icon: str
    default_port: int
    container_workdir: str
    image_tag: str
    features: list[str]
    env_vars: dict[str, str] = field(default_factory=dict)
    startup_command: list[str] = field(default_factory=list)

    def to_schema(self) -> TemplateInfo:
        return TemplateInfo(
            id=self.id,
            name=self.name,
            description=self.description,
            category=self.category,
            icon=self.icon,
            default_port=self.default_port,
            image_tag=self.image_tag,
            features=self.features,
        )


TEMPLATES: dict[str, WorkspaceTemplate] = {
    "vscode-empty": WorkspaceTemplate(
        id="vscode-empty",
        name="VS Code (Empty Project)",
        description="Clean, vanilla VS Code environment in the browser with terminal and git.",
        category="General",
        icon="code-bracket",
        default_port=8080,
        container_workdir="/home/coder/project",
        image_tag="localhost/devcloud-vscode-empty:latest",
        features=["VS Code Web (code-server)", "Git & Terminal", "Empty clean workspace"],
        env_vars={"DOCKER_USER": "coder"},
    ),
    "vscode-python": WorkspaceTemplate(
        id="vscode-python",
        name="VS Code (Python 3.14)",
        description="VS Code preloaded with Python 3.14/3.12, pip, uv, and VS Code Python extension.",
        category="Python",
        icon="code-bracket-square",
        default_port=8080,
        container_workdir="/home/coder/project",
        image_tag="localhost/devcloud-vscode-python:latest",
        features=["Python 3.14 Runtime", "VS Code Python Extension", "uv & pip", "Jupyter Interactive Extension"],
        env_vars={"DOCKER_USER": "coder"},
    ),
    "jupyter-python": WorkspaceTemplate(
        id="jupyter-python",
        name="Jupyter Notebook / Lab (Python)",
        description="Interactive JupyterLab environment with Python kernel and data science packages.",
        category="Data Science",
        icon="chart-bar",
        default_port=8888,
        container_workdir="/home/jovyan/work",
        image_tag="localhost/devcloud-jupyter-python:latest",
        features=["JupyterLab & Notebooks", "Python Kernel", "Data science stack", "Interactive plots"],
        env_vars={"JUPYTER_ENABLE_LAB": "yes"},
    ),
    "vscode-java": WorkspaceTemplate(
        id="vscode-java",
        name="VS Code (Java 21 LTS)",
        description="VS Code preloaded with OpenJDK 21, Maven, Gradle, and Red Hat Java Language Support.",
        category="Java",
        icon="command-line",
        default_port=8080,
        container_workdir="/home/coder/project",
        image_tag="localhost/devcloud-vscode-java:latest",
        features=["OpenJDK 21 LTS", "Language Support for Java", "Maven & Gradle preinstalled", "Java Debugger Extension"],
        env_vars={"DOCKER_USER": "coder"},
    ),
}


def get_template(template_id: str) -> WorkspaceTemplate | None:
    return TEMPLATES.get(template_id)


def list_templates() -> list[TemplateInfo]:
    return [template.to_schema() for template in TEMPLATES.values()]
