from __future__ import annotations

import json

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List


@dataclass
class CopyRule:
    source: str
    destination: str


@dataclass
class QtIfwComponent:
    component_id: str
    display_name: str
    description: str
    dependencies: List[str] = field(default_factory=list)
    replaces: List[str] = field(default_factory=list)
    copy_rules: List[CopyRule] = field(default_factory=list)
    default: bool = False
    update_text: str = ""
    metadata_package: str = ""
    use_hint_metadata: bool = False


@dataclass
class RepositoryDescriptor:
    source_name: str
    repo_name: str
    packet_name: str
    packaging_class: str
    qtifw_components: List[QtIfwComponent]


class BridgeConfiguration:
    def __init__(self, path: Path, raw_data: Dict[str, object]) -> None:
        self.path = path
        self.raw_data = raw_data
        self.defaults = raw_data.get("defaults", {})
        self.repositories = raw_data.get("repositories", {})

    @classmethod
    def load(cls, path: Path) -> "BridgeConfiguration":
        with path.open("r", encoding="utf-8") as file_handle:
            raw_data = json.load(file_handle)
        return cls(path=path, raw_data=raw_data)

    @property
    def python_root(self) -> str:
        return str(self.defaults.get("python_root", "apps/Python312"))

    def describe(
        self,
        source_name: str,
        package_names: Iterable[str],
    ) -> RepositoryDescriptor:
        package_list = list(package_names)
        repository_override = self.repositories.get(source_name, {})
        repo_name = str(
            repository_override.get(
                "repo_name",
                self._default_repo_name(source_name),
            )
        )
        packet_name = str(
            repository_override.get(
                "packet_name",
                self._default_packet_name(source_name),
            )
        )
        packaging_class = str(
            repository_override.get(
                "packaging_class",
                self._default_packaging_class(source_name),
            )
        )
        components = self._load_components(
            source_name=source_name,
            package_names=package_list,
            repository_override=repository_override,
            repo_name=repo_name,
            packet_name=packet_name,
            packaging_class=packaging_class,
        )
        return RepositoryDescriptor(
            source_name=source_name,
            repo_name=repo_name,
            packet_name=packet_name,
            packaging_class=packaging_class,
            qtifw_components=components,
        )

    def relevant_payload(self, source_name: str) -> Dict[str, object]:
        repository_override = self.repositories.get(source_name, {})
        return {
            "source_name": source_name,
            "defaults": self.defaults,
            "repository": repository_override,
        }

    def _default_repo_name(self, source_name: str) -> str:
        if source_name.startswith("python3-"):
            base_name = self._canonical_name(source_name[8:])
            return f"py_{base_name}"
        exceptions = set(self.defaults.get("repository_prefix_exceptions", []))
        if source_name in exceptions:
            return self._canonical_name(source_name)
        base_name = self._canonical_name(self._base_name(source_name))
        return f"lib_{base_name}"

    def _default_packet_name(self, source_name: str) -> str:
        if source_name.startswith("python3-"):
            return source_name[8:]
        return source_name

    def _default_packaging_class(self, source_name: str) -> str:
        if source_name.startswith("python3-"):
            return "python-site-package"
        return "osgeo4w-merge"

    def _load_components(
        self,
        source_name: str,
        package_names: List[str],
        repository_override: Dict[str, object],
        repo_name: str,
        packet_name: str,
        packaging_class: str,
    ) -> List[QtIfwComponent]:
        raw_components = repository_override.get("qtifw_components", [])
        default_metadata_package = self._default_metadata_package(package_names)
        if raw_components:
            return [
                self._component_from_raw(
                    raw_component=raw_component,
                    default_metadata_package=default_metadata_package,
                    default_use_hint_metadata=len(raw_components) == 1,
                )
                for raw_component in raw_components
            ]
        return [
            self._default_component(
                source_name=source_name,
                package_names=package_names,
                repo_name=repo_name,
                packet_name=packet_name,
                packaging_class=packaging_class,
            )
        ]

    def _component_from_raw(
        self,
        raw_component: Dict[str, object],
        default_metadata_package: str,
        default_use_hint_metadata: bool,
    ) -> QtIfwComponent:
        copy_rules = [
            CopyRule(
                source=str(rule["src"]),
                destination=str(rule["dst"]),
            )
            for rule in raw_component.get("copy_rules", [])
        ]
        return QtIfwComponent(
            component_id=str(raw_component["id"]),
            display_name=str(raw_component["display_name"]),
            description=str(raw_component.get("description", "")),
            dependencies=[
                str(item) for item in raw_component.get("dependencies", [])
            ],
            replaces=[
                str(item) for item in raw_component.get("replaces", [])
            ],
            copy_rules=copy_rules,
            default=bool(raw_component.get("default", False)),
            update_text=str(raw_component.get("update_text", "")),
            metadata_package=str(
                raw_component.get(
                    "metadata_package",
                    raw_component.get("package_name", default_metadata_package),
                )
            ),
            use_hint_metadata=bool(
                raw_component.get(
                    "use_hint_metadata",
                    default_use_hint_metadata,
                )
            ),
        )

    def _default_component(
        self,
        source_name: str,
        package_names: List[str],
        repo_name: str,
        packet_name: str,
        packaging_class: str,
    ) -> QtIfwComponent:
        component_token = self._component_token(source_name)
        if packaging_class == "python-site-package":
            component_id = f"com.nextgis.python.{component_token}"
            copy_rules = [
                CopyRule(
                    source="Lib/site-packages/**",
                    destination=f"{self.python_root}/Lib/site-packages",
                ),
                CopyRule(
                    source="Scripts/**",
                    destination=f"{self.python_root}/Scripts",
                ),
            ]
        else:
            component_id = f"com.nextgis.common.{component_token}"
            copy_rules = self._default_runtime_copy_rules(
                packet_name=packet_name,
                package_names=package_names,
                repo_name=repo_name,
            )
        return QtIfwComponent(
            component_id=component_id,
            display_name=self._display_name(source_name),
            description=f"Autogenerated component for {repo_name}",
            copy_rules=copy_rules,
            metadata_package=self._default_metadata_package(package_names),
            use_hint_metadata=True,
        )

    def _default_metadata_package(self, package_names: List[str]) -> str:
        runtime_packages = [
            package_name
            for package_name in package_names
            if not package_name.endswith("-devel")
        ]
        if runtime_packages:
            return runtime_packages[0]
        if package_names:
            return package_names[0]
        return ""

    def _default_runtime_copy_rules(
        self,
        packet_name: str,
        package_names: List[str],
        repo_name: str,
    ) -> List[CopyRule]:
        rules = [
            CopyRule(source="bin/*.dll", destination="bin"),
            CopyRule(source="bin/*.exe", destination="bin"),
            CopyRule(
                source=f"apps/{packet_name}/**",
                destination=f"apps/{packet_name}",
            ),
            CopyRule(
                source=f"share/{packet_name}/**",
                destination=f"share/{packet_name}",
            ),
            CopyRule(
                source=f"share/doc/{packet_name}/**",
                destination=f"share/doc/{packet_name}",
            ),
            CopyRule(
                source=f"etc/ini/{packet_name}.bat",
                destination="etc/ini",
            ),
        ]
        if repo_name.startswith("lib_") and package_names:
            rules.append(
                CopyRule(
                    source="share/translations/*.qm",
                    destination="share/translations",
                )
            )
        return rules

    def _display_name(self, source_name: str) -> str:
        token = self._component_token(source_name)
        return token.replace("_", " ").title()

    def _component_token(self, source_name: str) -> str:
        if source_name.startswith("python3-"):
            return self._canonical_name(source_name[8:])
        return self._canonical_name(self._base_name(source_name))

    def _canonical_name(self, name: str) -> str:
        normalized = name.replace("-", "_")
        alias_map = self.defaults.get("canonical_aliases", {})
        return str(alias_map.get(normalized, normalized))

    def _base_name(self, source_name: str) -> str:
        if source_name.startswith("lib") and len(source_name) > 3:
            return source_name[3:].lstrip("-")
        return source_name
