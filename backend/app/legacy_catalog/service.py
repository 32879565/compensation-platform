from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from hashlib import blake2b
from statistics import median
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.decimal import decimal_text
from app.importing.parser import clean_text, parse_money
from app.models.audit import AuditLog
from app.models.salary import SalaryRecord, SalarySource

LEGACY_SOURCES = (SalarySource.HISTORICAL, SalarySource.IMPORT)
MIN_GRADE_AGGREGATE_SIZE = 5


@dataclass(frozen=True)
class ComponentFieldSpec:
    source_field: str
    suggested_component_type: str | None
    classification: str
    importable: bool
    note: str


COMPONENT_FIELD_SPECS: tuple[ComponentFieldSpec, ...] = (
    ComponentFieldSpec(
        "综合薪资",
        "COMPREHENSIVE",
        "NEEDS_HR_CONFIRMATION",
        True,
        "历史字段可证明金额来源，但不能证明当前计薪政策。",
    ),
    ComponentFieldSpec(
        "该岗位综合薪资",
        "COMPREHENSIVE",
        "NEEDS_HR_CONFIRMATION",
        True,
        "部分原始工资表使用该表头；需与综合薪资口径核对。",
    ),
    ComponentFieldSpec(
        "基本薪资",
        "BASE",
        "NEEDS_HR_CONFIRMATION",
        True,
        "旧记录仅提供历史金额，不代表该字段直接参与应发工资。",
    ),
    *(
        ComponentFieldSpec(
            field,
            "POSITION",
            "NEEDS_HR_CONFIRMATION",
            True,
            "旧系统岗位金额可作岗位工资来源证据，当前政策仍需人事确认。",
        )
        for field in (
            "岗位工资",
            "传菜岗",
            "冷饮岗",
            "服务岗",
            "外卖岗",
            "收银岗",
            "前厅领班岗",
            "迎宾岗",
            "前厅经理岗",
            "领班岗",
            "经理岗",
        )
    ),
    ComponentFieldSpec(
        "房补",
        "HOUSING",
        "NEEDS_HR_CONFIRMATION",
        True,
        "字段名可识别为房补，但全额及折算规则仍需人事确认。",
    ),
    *(
        ComponentFieldSpec(
            field,
            "ALLOWANCE",
            "NEEDS_HR_CONFIRMATION",
            True,
            "历史明细无法证明固定或浮动属性，导入时必须由人事选择。",
        )
        for field in (
            "补贴",
            "深圳补贴",
            "春节6天补贴",
            "留守休息补贴",
            "通岗补贴",
            "留守出勤补贴",
            "留守出勤补贴 (N列)",
            "留守未出勤补贴",
            "过年未上班补贴",
            "化妆/饮品补贴",
            "车费报销",
            "带薪补贴",
            "春节补贴",
            "春节出勤补贴",
            "病假补贴",
        )
    ),
    *(
        ComponentFieldSpec(
            field,
            "PERFORMANCE",
            "NEEDS_HR_CONFIRMATION",
            True,
            "历史奖金金额可作来源证据，计算方式及生效规则仍需确认。",
        )
        for field in (
            "个人奖金",
            "门店奖金",
            "绩效奖金",
            "绩效",
            "外卖奖励",
            "全勤奖",
            "表现奖",
            "留年激励",
            "宿舍长奖励",
            "伯乐奖",
            "返岗激励",
            "奖励",
            "禁烟奖励",
            "春节激励",
            "刀工/其余奖罚",
        )
    ),
    *(
        ComponentFieldSpec(
            field,
            "DEDUCTION",
            "NEEDS_HR_CONFIRMATION",
            True,
            "历史扣减字段可作来源证据，适用条件仍需确认。",
        )
        for field in (
            "水电费",
            "空调费",
            "抽烟扣罚",
            "漏下单扣罚",
            "餐具破损",
            "餐具破損",
            "考勤扣罚",
            "绩效扣除",
            "扣罚",
            "留守扣除",
            "考勤实际扣罚",
        )
    ),
    *(
        ComponentFieldSpec(
            field,
            None,
            "DERIVED_NOT_CATALOG_COMPONENT",
            False,
            "该字段属于核算结果或法定扣缴，不应自动转成员工薪资结构组件。",
        )
        for field in (
            "出勤工资",
            "应发工资",
            "实发工资",
            "合计工资",
            "扣除后应发",
            "个人奖金系数",
            "奖金比例",
            "总奖金",
            "法定补贴",
            "法定补贴 (Y列)",
            "法定补贴 (Z列)",
            "法定补贴 (AA列)",
            "法定补贴 (AB列)",
            "法定补贴 (AC列)",
            "法定补贴 (AD列)",
            "法定补贴 (AE列)",
            "法定补贴 (AF列)",
            "法定补贴 (AG列)",
            "法定补贴 (AH列)",
            "法定补贴 (AI列)",
            "社保",
            "公积金",
            "个税",
            "押金",
            "上月工资",
        )
    ),
)


@dataclass(frozen=True)
class SourceSummary:
    record_count: int
    period_from: str | None
    period_to: str | None
    snapshot_id: str


@dataclass(frozen=True)
class ComponentObservation:
    source_field: str
    record_count: int
    nonzero_count: int
    period_from: str
    period_to: str


@dataclass(frozen=True)
class GradeObservation:
    position: str
    record_count: int
    contributor_count: int
    salary_sample_count: int
    period_from: str
    period_to: str
    observed_p25: str | None
    observed_median: str | None
    observed_p75: str | None
    suppressed_for_privacy: bool


@dataclass
class _ComponentAccumulator:
    record_count: int = 0
    nonzero_count: int = 0
    period_from: str | None = None
    period_to: str | None = None

    def add(self, *, period: str, raw_value: object) -> None:
        text = clean_text(raw_value)
        if not text:
            return
        self.record_count += 1
        parsed = parse_money(text)
        if parsed is not None and parsed != 0:
            self.nonzero_count += 1
        self.period_from = period if self.period_from is None else min(self.period_from, period)
        self.period_to = period if self.period_to is None else max(self.period_to, period)


@dataclass
class _GradeAccumulator:
    record_count: int = 0
    salaries_by_contributor: dict[str, list[Decimal]] = field(default_factory=dict)
    period_from: str | None = None
    period_to: str | None = None


def source_summary(session: Session) -> SourceSummary:
    count, period_from, period_to, max_id, last_updated = session.execute(
        select(
            func.count(SalaryRecord.id),
            func.min(SalaryRecord.period),
            func.max(SalaryRecord.period),
            func.max(SalaryRecord.id),
            func.max(SalaryRecord.updated_at),
        ).where(SalaryRecord.source.in_(LEGACY_SOURCES))
    ).one()
    snapshot_payload = "\0".join(
        (
            str(count or 0),
            period_from or "",
            period_to or "",
            str(max_id or 0),
            last_updated.isoformat() if last_updated is not None else "",
        )
    ).encode()
    return SourceSummary(
        record_count=int(count or 0),
        period_from=period_from,
        period_to=period_to,
        snapshot_id=blake2b(snapshot_payload, digest_size=16).hexdigest(),
    )


def _percentile_nearest(values: list[Decimal], ratio: Decimal) -> Decimal:
    if not values:
        raise ValueError("percentile requires at least one value")
    ordered = sorted(values)
    index = int((Decimal(len(ordered) - 1) * ratio).to_integral_value())
    return ordered[index]


def _stable_contributor_key(employee_id: int | None, emp_no: object) -> str | None:
    """Return a stable, non-PII key or exclude the row from salary evidence.

    Imported rows and reconciled historical rows carry ``employee_id``. The
    employee number is the safe fallback for older imported fixtures. Names
    are deliberately not used: aliases across months must never turn one
    person's history into five apparently independent contributors.
    """

    if employee_id is not None:
        return f"employee:{employee_id}"
    normalized_emp_no = clean_text(emp_no).casefold()
    return f"emp_no:{normalized_emp_no}" if normalized_emp_no else None


def catalog_observations(
    session: Session,
    *,
    include_suppressed_grades: bool = False,
) -> tuple[list[ComponentObservation], list[GradeObservation]]:
    component_accumulators = {
        spec.source_field: _ComponentAccumulator() for spec in COMPONENT_FIELD_SPECS
    }
    grade_accumulators: dict[str, _GradeAccumulator] = {}
    selected_fields = [spec.source_field for spec in COMPONENT_FIELD_SPECS]
    projections = [
        SalaryRecord.fields[field].astext.label(f"legacy_field_{index}")
        for index, field in enumerate(selected_fields)
    ]
    statement = (
        select(
            SalaryRecord.period,
            SalaryRecord.fields["职位"].astext.label("legacy_position"),
            SalaryRecord.fields["综合薪资"].astext.label("legacy_comprehensive"),
            SalaryRecord.employee_id.label("legacy_employee_id"),
            SalaryRecord.emp_no.label("legacy_emp_no"),
            *projections,
        )
        .where(SalaryRecord.source.in_(LEGACY_SOURCES))
        .execution_options(yield_per=1000)
    )
    for row in session.execute(statement):
        mapping = row._mapping
        period = mapping["period"]
        for index, source_field in enumerate(selected_fields):
            component_accumulators[source_field].add(
                period=period,
                raw_value=mapping[f"legacy_field_{index}"],
            )
        position = clean_text(mapping["legacy_position"])
        if not position:
            continue
        grade = grade_accumulators.setdefault(position, _GradeAccumulator())
        grade.record_count += 1
        grade.period_from = period if grade.period_from is None else min(grade.period_from, period)
        grade.period_to = period if grade.period_to is None else max(grade.period_to, period)
        salary = parse_money(mapping["legacy_comprehensive"])
        # Zero is a historical placeholder, not evidence of a real salary
        # level.  Excluding it keeps privacy counts and percentiles meaningful.
        contributor = _stable_contributor_key(
            mapping["legacy_employee_id"], mapping["legacy_emp_no"]
        )
        if salary is not None and salary > 0 and contributor:
            grade.salaries_by_contributor.setdefault(contributor, []).append(salary)

    component_observations = [
        ComponentObservation(
            source_field=source_field,
            record_count=accumulator.record_count,
            nonzero_count=accumulator.nonzero_count,
            period_from=accumulator.period_from or "",
            period_to=accumulator.period_to or "",
        )
        for source_field, accumulator in component_accumulators.items()
        if accumulator.record_count > 0
    ]
    grade_observations: list[GradeObservation] = []
    for position, accumulator in grade_accumulators.items():
        salaries = [
            Decimal(median(contributor_salaries))
            for contributor_salaries in accumulator.salaries_by_contributor.values()
        ]
        contributor_count = len(salaries)
        suppressed = contributor_count < MIN_GRADE_AGGREGATE_SIZE
        observation = GradeObservation(
            position=position,
            record_count=accumulator.record_count,
            contributor_count=contributor_count,
            salary_sample_count=sum(
                len(values) for values in accumulator.salaries_by_contributor.values()
            ),
            period_from=accumulator.period_from or "",
            period_to=accumulator.period_to or "",
            observed_p25=(
                None if suppressed else decimal_text(_percentile_nearest(salaries, Decimal("0.25")))
            ),
            observed_median=(None if suppressed else decimal_text(Decimal(median(salaries)))),
            observed_p75=(
                None if suppressed else decimal_text(_percentile_nearest(salaries, Decimal("0.75")))
            ),
            suppressed_for_privacy=suppressed,
        )
        if include_suppressed_grades or not observation.suppressed_for_privacy:
            grade_observations.append(observation)
    grade_observations.sort(key=lambda item: (-item.record_count, item.position))
    return component_observations, grade_observations


def component_observation(session: Session, source_field: str) -> ComponentObservation | None:
    observations, _ = catalog_observations(session)
    return next(
        (item for item in observations if item.source_field == source_field),
        None,
    )


def grade_observation(session: Session, position: str) -> GradeObservation | None:
    _, observations = catalog_observations(session, include_suppressed_grades=True)
    return next((item for item in observations if item.position == position), None)


CatalogSourceKind = Literal["component", "grade"]
_SOURCE_PROVENANCE: dict[CatalogSourceKind, tuple[str, str]] = {
    "component": ("legacy_catalog.component.apply", "source_field"),
    "grade": ("legacy_catalog.grade.apply", "source_position"),
}


def applied_source_targets(session: Session, *, kind: CatalogSourceKind) -> dict[str, int | None]:
    """Return successful immutable source claims for preview status rendering."""

    action, detail_key = _SOURCE_PROVENANCE[kind]
    return {
        source_value: target_id
        for source_value, target_id in session.execute(
            select(AuditLog.detail[detail_key].astext, AuditLog.target_id).where(
                AuditLog.action == action,
                AuditLog.result == "SUCCESS",
            )
        ).tuples()
        if source_value
    }


def _source_lock_key(kind: CatalogSourceKind, source_value: str) -> int:
    payload = f"compensation-platform:legacy-catalog:{kind}\0{source_value}".encode()
    digest = blake2b(payload, digest_size=8).digest()
    return int.from_bytes(digest, byteorder="big", signed=True)


def lock_and_check_source_applied(
    session: Session,
    *,
    kind: CatalogSourceKind,
    source_value: str,
) -> bool:
    """Serialize one legacy-source claim and consult append-only provenance.

    The transaction-level advisory lock closes the empty-row race: concurrent
    requests for the same source wait here, then the later request observes the
    first request's committed audit event.  A rolled-back attempt leaves no
    provenance and therefore does not consume the source.
    """
    session.execute(select(func.pg_advisory_xact_lock(_source_lock_key(kind, source_value)))).one()
    action, detail_key = _SOURCE_PROVENANCE[kind]
    applied_id = session.scalar(
        select(AuditLog.id)
        .where(
            AuditLog.action == action,
            AuditLog.result == "SUCCESS",
            AuditLog.detail[detail_key].astext == source_value,
        )
        .limit(1)
    )
    return applied_id is not None
