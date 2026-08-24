from dataclasses import dataclass
from app.schemas.workspace import FlavorInfo


@dataclass(frozen=True)
class Flavor:
    id: str
    name: str
    description: str
    cpus: float
    memory_mb: int
    memory_display: str

    def to_schema(self) -> FlavorInfo:
        return FlavorInfo(
            id=self.id,
            name=self.name,
            description=self.description,
            cpus=self.cpus,
            memory_mb=self.memory_mb,
            memory_display=self.memory_display,
        )


FLAVORS: dict[str, Flavor] = {
    "t1.nano": Flavor(
        id="t1.nano",
        name="t1.nano",
        description="Hafif script, küçük yapılandırma ve düşük RAM gerektiren işler için",
        cpus=0.5,
        memory_mb=512,
        memory_display="512 MB",
    ),
    "t1.micro": Flavor(
        id="t1.micro",
        name="t1.micro",
        description="Python ve temel uygulamalar için standart tek thread geliştirme ortamı",
        cpus=1.0,
        memory_mb=1024,
        memory_display="1 GB",
    ),
    "t1.mini": Flavor(
        id="t1.mini",
        name="t1.mini",
        description="Java build, notebook ve çok thread kullanan araçlar için iki kat işlem gücü",
        cpus=2.0,
        memory_mb=2048,
        memory_display="2 GB",
    ),
}


def get_flavor(flavor_id: str) -> Flavor | None:
    return FLAVORS.get(flavor_id)


def list_flavors() -> list[FlavorInfo]:
    return [flavor.to_schema() for flavor in FLAVORS.values()]
