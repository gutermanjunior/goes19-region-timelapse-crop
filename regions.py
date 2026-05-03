from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CropRegion:
    name: str
    left: int
    top: int
    width: int
    height: int
    description: str = ""

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def bottom(self) -> int:
        return self.top + self.height

    @property
    def box(self) -> tuple[int, int, int, int]:
        return (self.left, self.top, self.right, self.bottom)

    def clamp(self, image_width: int, image_height: int) -> "CropRegion":
        left = max(0, self.left)
        top = max(0, self.top)
        right = min(image_width, self.right)
        bottom = min(image_height, self.bottom)

        width = max(0, right - left)
        height = max(0, bottom - top)

        return CropRegion(
            name=self.name,
            left=left,
            top=top,
            width=width,
            height=height,
            description=self.description,
        )


# Valores iniciais de EXEMPLO.
# Ajuste depois que você validar visualmente a posição correta da sua região.
REGIONS: dict[str, CropRegion] = {
    "manual_brasil_sudeste": CropRegion(
        name="manual_brasil_sudeste",
        left=11800,
        top=5400,
        width=4200,
        height=2800,
        description="Recorte manual de exemplo para o Sudeste. Ajuste fino necessário.",
    ),
    "manual_topo_teste": CropRegion(
        name="manual_topo_teste",
        left=9000,
        top=2500,
        width=5000,
        height=2500,
        description="Faixa superior de teste para validar coordenadas rapidamente.",
    ),
    "full_disk": CropRegion(
        name="full_disk",
        left=0,
        top=0,
        width=21696,
        height=21696,
        description="Imagem completa. Útil só para validação.",
    ),"sudeste_brasil": CropRegion(
    name="sudeste_brasil",
        left=14400,
        top=14600,
        width=3100,
        height=1600,
        description="Recorte focado na região Sudeste do Brasil.",
    ),
}


def get_region(name: str) -> CropRegion:
    try:
        return REGIONS[name]
    except KeyError as exc:
        valid = ", ".join(sorted(REGIONS))
        raise KeyError(f"Região '{name}' não encontrada. Opções: {valid}") from exc