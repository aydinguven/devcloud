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
    accelerator_count: int = 0
    accelerator_vendor: str = ""
    accelerator_memory_mb: int = 0
    accelerator_display: str = ""
    selectable: bool = True

    def to_schema(self) -> FlavorInfo:
        return FlavorInfo(
            id=self.id,
            name=self.name,
            description=self.description,
            cpus=self.cpus,
            memory_mb=self.memory_mb,
            memory_display=self.memory_display,
            accelerator_count=self.accelerator_count,
            accelerator_vendor=self.accelerator_vendor,
            accelerator_memory_mb=self.accelerator_memory_mb,
            accelerator_display=self.accelerator_display,
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
    "t1.small": Flavor(
        id="t1.small",
        name="t1.small",
        description="Python, React/Node.js ve standart VS Code geliştirme ortamları için",
        cpus=1.0,
        memory_mb=2048,
        memory_display="2 GB",
    ),
    "t1.mini": Flavor(
        id="t1.mini",
        name="t1.mini",
        description="Mevcut çalışma alanlarıyla geriye dönük uyumluluk için korunan eski profil",
        cpus=2.0,
        memory_mb=2048,
        memory_display="2 GB",
        selectable=False,
    ),
    "t1.medium": Flavor(
        id="t1.medium",
        name="t1.medium",
        description="Java, Jupyter ve orta ölçekli build işlemleri için",
        cpus=2.0,
        memory_mb=4096,
        memory_display="4 GB",
    ),
    "t1.large": Flavor(
        id="t1.large",
        name="t1.large",
        description="Ağır build, veri analizi ve birden fazla servis için",
        cpus=4.0,
        memory_mb=8192,
        memory_display="8 GB",
    ),
    "t1.xlarge": Flavor(
        id="t1.xlarge",
        name="t1.xlarge",
        description="Yoğun derleme ve büyük notebook iş yükleri için",
        cpus=8.0,
        memory_mb=16384,
        memory_display="16 GB",
    ),
    "g1.shared": Flavor(
        id="g1.shared",
        name="g1.shared",
        description=(
            "RTX 4090/5090 üzerinde paylaşımlı slot veya B300 üzerinde "
            "izole MIG aygıtı; GPU belleği limiti best-effort'tur"
        ),
        cpus=4.0,
        memory_mb=16384,
        memory_display="16 GB",
        accelerator_count=1,
        accelerator_vendor="nvidia",
        accelerator_memory_mb=8192,
        accelerator_display="1 NVIDIA GPU · 8 GB+",
    ),
}


def get_flavor(flavor_id: str) -> Flavor | None:
    return FLAVORS.get(flavor_id)


def list_flavors() -> list[FlavorInfo]:
    return [
        flavor.to_schema()
        for flavor in FLAVORS.values()
        if flavor.selectable
    ]
