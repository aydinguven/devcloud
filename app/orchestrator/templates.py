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
        name="Boş Proje",
        description="Tarayıcıda terminal ve Git içeren temiz, standart VS Code ortamı.",
        category="Genel",
        icon="code-bracket",
        default_port=8080,
        container_workdir="/home/coder/project",
        image_tag="localhost/devcloud-vscode-empty:latest",
        features=["VS Code Web (code-server)", "Git & Terminal", "Boş ve temiz çalışma alanı"],
        env_vars={"DOCKER_USER": "coder"},
    ),
    "vscode-python": WorkspaceTemplate(
        id="vscode-python",
        name="Python 3.14",
        description="Python 3.14/3.12, pip, uv ve VS Code Python extension yüklü geliştirme ortamı.",
        category="Python",
        icon="code-bracket-square",
        default_port=8080,
        container_workdir="/home/coder/project",
        image_tag="localhost/devcloud-vscode-python:latest",
        features=["Python 3.14 Runtime", "VS Code Python Extension", "uv & pip", "Jupyter Interactive Extension"],
        env_vars={"DOCKER_USER": "coder"},
    ),
    "vscode-react": WorkspaceTemplate(
        id="vscode-react",
        name="React/Node.js",
        description="Node.js 22 LTS, package manager ve React/TypeScript araçları yüklü VS Code ortamı.",
        category="Web Geliştirme",
        icon="code-bracket-square",
        default_port=8080,
        container_workdir="/home/coder/project",
        image_tag="localhost/devcloud-vscode-react:latest",
        features=["Node.js 22 LTS", "React & TypeScript Tooling", "npm, pnpm & Yarn", "ESLint & Prettier Extensions"],
        env_vars={"DOCKER_USER": "coder"},
    ),
    "jupyter-python": WorkspaceTemplate(
        id="jupyter-python",
        name="Jupyter Notebook",
        description="Python kernel ve veri bilimi paketleri içeren etkileşimli JupyterLab ortamı.",
        category="Veri Bilimi",
        icon="chart-bar",
        default_port=8888,
        container_workdir="/home/jovyan/work",
        image_tag="localhost/devcloud-jupyter-python:latest",
        features=["JupyterLab & Notebooks", "Python Kernel", "Veri bilimi araçları", "Etkileşimli grafikler"],
        env_vars={"JUPYTER_ENABLE_LAB": "yes"},
    ),
    "vscode-java": WorkspaceTemplate(
        id="vscode-java",
        name="Java 21 LTS",
        description="OpenJDK 21, Maven, Gradle ve Red Hat Java Language Support yüklü VS Code ortamı.",
        category="Java",
        icon="command-line",
        default_port=8080,
        container_workdir="/home/coder/project",
        image_tag="localhost/devcloud-vscode-java:latest",
        features=["OpenJDK 21 LTS", "Language Support for Java", "Maven & Gradle", "Java Debugger Extension"],
        env_vars={"DOCKER_USER": "coder"},
    ),
}


def get_template(template_id: str) -> WorkspaceTemplate | None:
    return TEMPLATES.get(template_id)


def list_templates() -> list[TemplateInfo]:
    return [template.to_schema() for template in TEMPLATES.values()]
