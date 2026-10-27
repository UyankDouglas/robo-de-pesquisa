"""Correspondência CNPJ + Número a portar e classificação pelo "Status do Bilhete".

Prioridade máxima: nunca aplicar a uma linha o status do bilhete de outra. Por isso:
- só há correspondência quando CNPJ (14 dígitos) E número (dígitos normalizados) são idênticos;
- o "Status Solicitação" da listagem nunca é usado para classificar;
- vários bilhetes para o mesmo número só são resolvidos pela data mais recente comprovada (se
  configurado) ou quando todos dizem a mesma coisa; caso contrário a linha vai para revisão.
"""
from __future__ import annotations

import datetime as dt
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable

from core.models import ItemStatus
from phoenix.config import MatchRules, StatusRules
from phoenix.normalize import PhoneRules, normalize_label, normalize_phone
from phoenix.workbook import SourceRow


class Category(str, Enum):
    EM_ANDAMENTO = "EM_ANDAMENTO"
    FALHA_PARCIAL_PORTADA = "FALHA_PARCIAL_PORTADA"
    EM_CONFLITO = "EM_CONFLITO"
    OUTRO = "OUTRO"


CATEGORY_LABELS = {
    Category.EM_ANDAMENTO: "Em andamento",
    Category.FALHA_PARCIAL_PORTADA: "Falha parcial / portada",
    Category.EM_CONFLITO: "Em conflito",
    Category.OUTRO: "Outros status",
}


class Outcome(str, Enum):
    CLASSIFICADO = "Classificado"
    OUTRO_STATUS = "Outro status (ignorado)"
    SEM_CORRESPONDENCIA = "Sem correspondência"
    CNPJ_SEM_SOLICITACOES = "CNPJ sem solicitações"
    REVISAR = "Revisar"
    SEM_NUMERO = "Linha sem número de acesso"
    CNPJ_INVALIDO = "CNPJ inválido"
    ERRO_CONSULTA = "Erro na consulta"
    PENDENTE = "Pendente"


@dataclass
class TicketRef:
    cnpj: str
    solicitacao_id: str
    solicitacao: dict[str, str]
    data: dt.datetime | None
    index: int
    fields: dict[str, str]
    numero: str
    temp: str

    @property
    def status(self) -> str:
        return self.fields.get("status_bilhete", "").strip()

    @property
    def motivo(self) -> str:
        return self.fields.get("motivo_recusa", "").strip()


@dataclass
class RowResult:
    row: SourceRow
    outcome: Outcome
    category: Category | None = None
    ticket: TicketRef | None = None
    fila_nova: str | None = None
    revisao: list[str] = field(default_factory=list)
    via: str = ""
    candidatos: int = 0


@dataclass
class CnpjQuery:
    status: ItemStatus
    result: dict[str, Any] | None = None
    message: str | None = None
    queried_at: str | None = None


@dataclass
class MatchReport:
    rows: list[RowResult]
    unmatched_tickets: list[TicketRef]
    tickets_analyzed: int
    ticket_status_counts: Counter

    def by_category(self, category: Category) -> list[RowResult]:
        return [r for r in self.rows if r.outcome == Outcome.CLASSIFICADO and r.category == category]

    def count(self, outcome: Outcome) -> int:
        return sum(1 for r in self.rows if r.outcome == outcome)

    def summary(self) -> dict[str, int]:
        return {
            "linhas": len(self.rows),
            "em_andamento": len(self.by_category(Category.EM_ANDAMENTO)),
            "falha_parcial_portada": len(self.by_category(Category.FALHA_PARCIAL_PORTADA)),
            "em_conflito": len(self.by_category(Category.EM_CONFLITO)),
            "outros_status": self.count(Outcome.OUTRO_STATUS),
            "sem_correspondencia": self.count(Outcome.SEM_CORRESPONDENCIA),
            "cnpj_sem_solicitacoes": self.count(Outcome.CNPJ_SEM_SOLICITACOES),
            "revisar": self.count(Outcome.REVISAR) + sum(1 for r in self.rows if r.outcome == Outcome.CLASSIFICADO and r.revisao),
            "sem_numero": self.count(Outcome.SEM_NUMERO),
            "cnpj_invalido": self.count(Outcome.CNPJ_INVALIDO),
            "erro_consulta": self.count(Outcome.ERRO_CONSULTA),
            "pendentes": self.count(Outcome.PENDENTE),
            "bilhetes_analisados": self.tickets_analyzed,
            "bilhetes_sem_linha": len(self.unmatched_tickets),
        }


_DATE = re.compile(r"(\d{2})/(\d{2})/(\d{4})(?:\s+(\d{2}):(\d{2})(?::(\d{2}))?)?")


def parse_date(text: str) -> dt.datetime | None:
    match = _DATE.search(text or "")
    if not match:
        return None
    day, month, year, hour, minute, second = match.groups()
    try:
        return dt.datetime(int(year), int(month), int(day), int(hour or 0), int(minute or 0), int(second or 0))
    except ValueError:
        return None


def _field(data: dict[str, str], label: str) -> str:
    wanted = normalize_label(label)
    return next((v for k, v in data.items() if normalize_label(k) == wanted), "")


def flatten_tickets(cnpj: str, result: dict[str, Any] | None, phone: PhoneRules) -> list[TicketRef]:
    tickets = []
    for sol in (result or {}).get("solicitacoes", []):
        dados = sol.get("dados", {})
        for index, fields_ in enumerate(sol.get("bilhetes", [])):
            tickets.append(TicketRef(
                cnpj=cnpj,
                solicitacao_id=str(sol.get("id", "")),
                solicitacao=dados,
                data=parse_date(_field(dados, "Data Solicitação")),
                index=index,
                fields=fields_,
                numero=normalize_phone(fields_.get("numero_portar", ""), phone),
                temp=normalize_phone(fields_.get("numero_temporario", ""), phone),
            ))
    return tickets


def classify_status(status: str, rules: StatusRules) -> Category:
    value = normalize_label(status)
    for category, options in ((Category.EM_ANDAMENTO, rules.em_andamento),
                              (Category.FALHA_PARCIAL_PORTADA, rules.falha_parcial_portada),
                              (Category.EM_CONFLITO, rules.em_conflito)):
        if value and value in {normalize_label(o) for o in options}:
            return category
    return Category.OUTRO


def _same_meaning(tickets: list[TicketRef]) -> bool:
    return len({(normalize_label(t.status), t.motivo) for t in tickets}) == 1


def _choose(candidates: list[TicketRef], rules: MatchRules) -> tuple[TicketRef | None, str]:
    """Escolhe o bilhete entre vários do mesmo número, ou devolve o motivo da revisão."""
    dated = [t for t in candidates if t.data]
    latest = max((t.data for t in dated), default=None)
    newest = [t for t in dated if t.data == latest]
    if _same_meaning(candidates):
        return (newest[0] if len(newest) == 1 else candidates[0]), ""
    if rules.multiplos_bilhetes != "mais_recente":
        return None, f"{len(candidates)} bilhetes para o mesmo número com status diferentes (regra: revisar)."
    if len(dated) != len(candidates):
        return None, f"{len(candidates)} bilhetes para o mesmo número e nem todos têm data de solicitação legível."
    if len(newest) != 1:
        return None, f"{len(candidates)} bilhetes para o mesmo número com a mesma data mais recente: ambíguo."
    return newest[0], ""


def match(
    rows: Iterable[SourceRow],
    queries: dict[str, CnpjQuery],
    status_rules: StatusRules,
    match_rules: MatchRules,
) -> MatchReport:
    phone = PhoneRules(strip_ddi_55=match_rules.strip_ddi_55, strip_trunk_zero=match_rules.strip_trunk_zero)
    rows = list(rows)
    tickets_by_cnpj: dict[str, list[TicketRef]] = {}
    for cnpj, query in queries.items():
        if query.status == ItemStatus.FOUND:
            tickets_by_cnpj[cnpj] = flatten_tickets(cnpj, query.result, phone)

    duplicates: dict[tuple, list[int]] = defaultdict(list)
    for row in rows:
        if row.cnpj.value and row.acesso:
            duplicates[(row.sheet, row.cnpj.value, row.acesso)].append(row.row)

    used: set[tuple[str, str, int]] = set()
    results: list[RowResult] = []
    for row in rows:
        res = RowResult(row, Outcome.PENDENTE, revisao=list(row.warnings))
        results.append(res)
        if row.cnpj.status != "valido":
            res.outcome = Outcome.CNPJ_INVALIDO
            res.revisao.append(row.cnpj.message)
            continue
        query = queries.get(row.cnpj.value)
        if query is None or query.status in (ItemStatus.PENDING, ItemStatus.PROCESSING):
            continue
        if query.status == ItemStatus.ERROR:
            res.outcome = Outcome.ERRO_CONSULTA
            res.revisao.append(query.message or "Falha na consulta")
            continue
        if query.status == ItemStatus.NOT_FOUND:
            res.outcome = Outcome.CNPJ_SEM_SOLICITACOES
            continue
        if not row.acesso:
            res.outcome = Outcome.SEM_NUMERO
            continue
        same = duplicates.get((row.sheet, row.cnpj.value, row.acesso), [])
        if len(same) > 1:
            res.revisao.append(f"Mesmo CNPJ + número em mais de uma linha desta aba (linhas {', '.join(map(str, same))}).")

        tickets = tickets_by_cnpj.get(row.cnpj.value, [])
        candidates = [t for t in tickets if t.numero == row.acesso]
        if not candidates and match_rules.usar_numero_temporario and row.temp:
            by_temp = [t for t in tickets if t.temp and t.temp == row.temp]
            if len(by_temp) == 1:
                candidates, res.via = by_temp, "número temporário"
            elif len(by_temp) > 1:
                res.outcome = Outcome.REVISAR
                res.revisao.append(f"Número temporário corresponde a {len(by_temp)} bilhetes.")
                continue
        res.candidatos = len(candidates)
        if not candidates:
            res.outcome = Outcome.SEM_CORRESPONDENCIA
            continue
        chosen, problem = (candidates[0], "") if len(candidates) == 1 else _choose(candidates, match_rules)
        for t in candidates:
            used.add((t.solicitacao_id, t.cnpj, t.index))
        if chosen is None:
            res.outcome = Outcome.REVISAR
            res.revisao.append(problem)
            continue
        res.ticket = chosen
        res.category = classify_status(chosen.status, status_rules)
        if res.category == Category.OUTRO:
            res.outcome = Outcome.OUTRO_STATUS
            continue
        res.outcome = Outcome.CLASSIFICADO
        if res.category == Category.EM_ANDAMENTO:
            res.fila_nova = status_rules.texto_fila_em_andamento
        elif res.category == Category.FALHA_PARCIAL_PORTADA:
            res.fila_nova = chosen.status
        else:
            empty = {normalize_label(v) for v in status_rules.motivo_recusa_vazio}
            if normalize_label(chosen.motivo) in empty:
                res.fila_nova = chosen.status
                res.revisao.append("Em conflito sem Motivo Recusa preenchido: motivo pendente, revisar.")
            else:
                res.fila_nova = chosen.motivo

    all_tickets = [t for ts in tickets_by_cnpj.values() for t in ts]
    unmatched = [t for t in all_tickets if (t.solicitacao_id, t.cnpj, t.index) not in used]
    status_counts = Counter(t.status or "(vazio)" for t in all_tickets)
    return MatchReport(results, unmatched, len(all_tickets), status_counts)
