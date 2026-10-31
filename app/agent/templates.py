"""
The templates an agent may send.

Only relevant outside the 24-hour window: inside it an agent replies in free text, and
outside it WhatsApp accepts approved templates only. So this is the picker the app
shows when the composer is disabled.

**Structure is resolved live, never snapshotted.** The mapping stores an id; body text,
parameters, header and buttons are read from `whatsapp_templates` on every call. Meta
rewrites `components` wholesale on every sync and a re-approval can add or remove a
parameter — a copy taken at mapping time would quietly stop matching what Meta accepts,
and the only symptom would be a rejected send with a parameter-count error.

**Only APPROVED templates are returned.** A template paused or rejected at Meta vanishes
from the picker rather than letting an agent fill in a form for a message that cannot be
delivered. The admin panel still shows it, flagged, so somebody can see why an agent
lost one.
"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.agent.deps import AgentContext, get_agent_session
from app.core import template_body
from app.core.database import get_db
from app.models.agent import AgentTemplate
from app.models.whatsapp import WhatsAppTemplate

router = APIRouter(prefix="/templates", tags=["Agent App"])


class TemplateParam(BaseModel):
    index:   int
    label:   str
    example: str


class TemplateHeader(BaseModel):
    # None | TEXT | IMAGE | DOCUMENT | VIDEO
    format: str | None
    # Only for a TEXT header. A media header's file is supplied at send time.
    text:   str | None


class TemplateButton(BaseModel):
    type: str
    text: str


class AgentTemplateOut(BaseModel):
    """
    Everything the app needs to render a template and send it.

    `name` + `language` are the send address — Meta identifies a template by those, not
    by our `id`, which is why both are here even though `id` is the stable key.
    """
    id:          str
    name:        str
    language:    str
    category:    str
    body_text:   str
    param_count: int
    parameters:  list[TemplateParam]
    header:      TemplateHeader
    footer_text: str | None
    buttons:     list[TemplateButton]


@router.get("", response_model=list[AgentTemplateOut])
def list_agent_templates(
    ctx: AgentContext = Depends(get_agent_session),
    db: Session = Depends(get_db),
):
    """
    The approved templates mapped to this agent.

    An empty list means none have been assigned yet — not that something failed. The app
    should say so plainly and point at the administrator, because a template send is a
    billable message and defaulting to "all of them" would hand every new agent the
    company's entire catalogue.
    """
    rows = (
        db.query(WhatsAppTemplate)
        .join(AgentTemplate, AgentTemplate.template_id == WhatsAppTemplate.id)
        .filter(
            AgentTemplate.agent_id == ctx.agent_row_id,
            WhatsAppTemplate.status == "APPROVED",
            # Defence in depth. The admin API already refuses to map a template from
            # another company, but a user moved between companies would otherwise keep
            # reaching their old employer's templates.
            WhatsAppTemplate.company_id == ctx.company_id,
        )
        .order_by(WhatsAppTemplate.name)
        .all()
    )
    return [_describe(t) for t in rows]


def _describe(t: WhatsAppTemplate) -> AgentTemplateOut:
    comps = t.components or []
    return AgentTemplateOut(
        id=str(t.id),
        name=t.name,
        language=t.language,
        category=t.category,
        body_text=template_body.body_text(comps),
        param_count=template_body.param_count(comps),
        parameters=[
            TemplateParam(index=p["index"], label=p["label"], example=p["example"])
            # param_mapping is authored for the client ingest API, but where it exists
            # it gives a far better label than "Param 3", so it is reused here.
            for p in template_body.describe_params(comps, t.param_mapping)
        ],
        header=TemplateHeader(
            format=template_body.header_format(comps),
            text=template_body.header_text(comps),
        ),
        footer_text=template_body.footer_text(comps),
        buttons=[TemplateButton(**b) for b in template_body.buttons(comps)],
    )
