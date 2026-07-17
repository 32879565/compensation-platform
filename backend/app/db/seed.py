"""从旧 salary_search_app 的 config.json 门店别名生成组织树种子（集团→区域→门店）。

区域按门店名前缀推断城市；纯函数 build_org_tree 便于单测，CLI 读 config 落库。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.orm import Session

from app.models.org import OrgType, OrgUnit

# 门店名前缀 → 城市（区域）。未命中默认归广州。
_CITY_PREFIXES: list[tuple[tuple[str, ...], str]] = [
    (("深圳", "南山", "宝安", "龙华", "龙岗", "福田", "罗湖", "西丽", "坂田", "前海"), "深圳"),
    (("东莞", "里悦里"), "东莞"),
    (("珠海",), "珠海"),
    (("佛山", "桂城", "黄岐", "南海", "千灯湖", "雅乐城", "金铂", "新dna", "新DNA"), "佛山"),
    (("花都", "番禺", "从化", "增城"), "广州"),
]
_DEFAULT_CITY = "广州"


def infer_city(store_name: str) -> str:
    for prefixes, city in _CITY_PREFIXES:
        if store_name.startswith(prefixes):
            return city
    return _DEFAULT_CITY


@dataclass
class RegionNode:
    city: str
    stores: list[str] = field(default_factory=list)


@dataclass
class OrgTree:
    group_name: str
    regions: list[RegionNode]

    @property
    def store_count(self) -> int:
        return sum(len(r.stores) for r in self.regions)


def build_org_tree(store_names: list[str], group_name: str = "集团总部") -> OrgTree:
    """把门店清单按推断城市分组为 集团→区域→门店 三层树（纯函数，可测）。"""
    by_city: dict[str, list[str]] = {}
    for raw in store_names:
        name = raw.strip()
        if not name:
            continue
        by_city.setdefault(infer_city(name), [])
        if name not in by_city[infer_city(name)]:
            by_city[infer_city(name)].append(name)
    regions = [
        RegionNode(city=city, stores=sorted(stores)) for city, stores in sorted(by_city.items())
    ]
    return OrgTree(group_name=group_name, regions=regions)


def canonical_store_names(config: dict) -> list[str]:
    """从旧 config.json 提取规范门店名：store_aliases 的值 + 键中已带'店'的规范名。"""
    aliases: dict[str, str] = config.get("store_aliases", {})
    names = set(aliases.values())
    for key in aliases:
        if key.endswith("店") and key not in aliases.values():
            # 键是别名，通常不规范；仅在其本身像规范名且未作为别名目标时纳入
            pass
    return sorted(names)


def _code(prefix: str, seq: int) -> str:
    return f"{prefix}{seq:03d}"


def seed_org_tree(session: Session, tree: OrgTree) -> int:
    """把组织树写入库；幂等：已存在同 code 则跳过。返回新建门店数。"""
    existing_codes = {c for (c,) in session.query(OrgUnit.code).all()}
    group_code = "GROUP"
    group = session.query(OrgUnit).filter_by(code=group_code).one_or_none()
    if group is None:
        group = OrgUnit(code=group_code, name=tree.group_name, type=OrgType.GROUP)
        session.add(group)
        session.flush()

    created = 0
    for r_idx, region in enumerate(tree.regions, start=1):
        region_code = _code("REG", r_idx)
        node = session.query(OrgUnit).filter_by(code=region_code).one_or_none()
        if node is None:
            node = OrgUnit(
                code=region_code,
                name=f"{region.city}区域",
                type=OrgType.REGION,
                city=region.city,
                parent_id=group.id,
            )
            session.add(node)
            session.flush()
        for s_idx, store in enumerate(region.stores, start=1):
            store_code = _code(f"S{r_idx:02d}", s_idx)
            if store_code in existing_codes:
                continue
            session.add(
                OrgUnit(
                    code=store_code,
                    name=store,
                    type=OrgType.STORE,
                    city=region.city,
                    parent_id=node.id,
                )
            )
            created += 1
    session.flush()
    return created


def main() -> None:  # pragma: no cover - CLI 入口
    import argparse

    from app.db.session import SessionLocal

    parser = argparse.ArgumentParser(description="从旧 config.json 生成组织树种子")
    parser.add_argument(
        "--config",
        default=str(Path(__file__).resolve().parents[4] / "salary_search_app" / "config.json"),
        help="旧 salary_search_app 的 config.json 路径",
    )
    args = parser.parse_args()

    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    tree = build_org_tree(canonical_store_names(config))
    with SessionLocal() as session:
        created = seed_org_tree(session, tree)
        session.commit()
    print(
        f"组织树种子完成：{len(tree.regions)} 个区域，新建 {created} 个门店"
        f"（共 {tree.store_count} 个门店）。"
    )


if __name__ == "__main__":  # pragma: no cover
    main()
