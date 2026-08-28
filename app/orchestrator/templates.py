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

BUILTIN_TEMPLATE_IDS = tuple(TEMPLATES)


def get_template(template_id: str) -> WorkspaceTemplate | None:
    return TEMPLATES.get(template_id)


def list_templates() -> list[TemplateInfo]:
    return [template.to_schema() for template in TEMPLATES.values()]


def list_builtin_templates() -> list[TemplateInfo]:
    return [TEMPLATES[template_id].to_schema() for template_id in BUILTIN_TEMPLATE_IDS]


def register_custom_template(
    template_id: str,
    name: str,
    description: str,
    category: str,
    image_tag: str,
    default_port: int = 8080,
    ide_type: str = "vscode",
    icon: str = "cube",
) -> WorkspaceTemplate:
    """Register a custom template into the runtime registry."""
    workdir = "/home/jovyan/work" if ide_type == "jupyter" else "/home/coder/project"
    features = ["Özel Şablon", f"Port: {default_port}", f"Görüntü: {image_tag}"]
    
    tpl = WorkspaceTemplate(
        id=template_id,
        name=name,
        description=description,
        category=category,
        icon=icon,
        default_port=default_port,
        container_workdir=workdir,
        image_tag=image_tag,
        features=features,
        env_vars={"DOCKER_USER": "coder"} if ide_type != "jupyter" else {"JUPYTER_ENABLE_LAB": "yes"},
    )
    TEMPLATES[template_id] = tpl
    return tpl


async def resolve_template(db, template_id: str) -> WorkspaceTemplate | None:
    """Lookup template from memory or load from custom_templates database table."""
    if template_id in TEMPLATES:
        return TEMPLATES[template_id]

    from sqlalchemy import select
    from app.models.custom_template import CustomTemplate

    stmt = select(CustomTemplate).where(CustomTemplate.id == template_id)
    res = await db.execute(stmt)
    db_tpl = res.scalar_one_or_none()
    if db_tpl:
        return register_custom_template(
            template_id=db_tpl.id,
            name=db_tpl.name,
            description=db_tpl.description,
            category=db_tpl.category,
            image_tag=db_tpl.image_tag,
            default_port=db_tpl.default_port,
            ide_type=db_tpl.ide_type,
            icon=db_tpl.icon,
        )
    return None
