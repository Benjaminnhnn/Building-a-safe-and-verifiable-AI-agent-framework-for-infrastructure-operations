"""Generate docs/diagrams/system-design-aws.drawio (AWS deployment + AI agent framework pages).

Usage: python docs/diagrams/generate_system_design.py docs/diagrams/system-design-aws.drawio
Open the output in draw.io / diagrams.net; it uses the built-in AWS 2021+ (aws4) icon set.
"""

import sys
from xml.sax.saxutils import escape

OUT = sys.argv[1]

FONT = "fontFamily=Helvetica;"
GROUP_BASE = (
    "points=[[0,0],[0.25,0],[0.5,0],[0.75,0],[1,0],[1,0.25],[1,0.5],[1,0.75],[1,1],[0.75,1],[0.5,1],"
    "[0.25,1],[0,1],[0,0.75],[0,0.5],[0,0.25]];outlineConnect=0;gradientColor=none;html=1;whiteSpace=wrap;"
    "fontSize=13;fontStyle=1;container=0;pointerEvents=0;collapsible=0;recursiveResize=0;shape=mxgraph.aws4.group;"
    "verticalAlign=top;align=left;spacingLeft=30;dashed=0;" + FONT
)
GROUPS = {
    "cloud": GROUP_BASE + "grIcon=mxgraph.aws4.group_aws_cloud_alt;strokeColor=#232F3E;fillColor=none;fontColor=#232F3E;",
    "region": GROUP_BASE + "grIcon=mxgraph.aws4.group_region;strokeColor=#00A4A6;fillColor=none;fontColor=#147EBA;dashed=1;",
    "vpc": GROUP_BASE + "grIcon=mxgraph.aws4.group_vpc2;strokeColor=#8C4FFF;fillColor=none;fontColor=#8C4FFF;",
    "public": GROUP_BASE + "grIcon=mxgraph.aws4.group_security_group;grStroke=0;strokeColor=#7AA116;fillColor=#F2F6E8;fontColor=#248814;",
    "private": GROUP_BASE + "grIcon=mxgraph.aws4.group_security_group;grStroke=0;strokeColor=#00A4A6;fillColor=#E6F6F7;fontColor=#147EBA;",
    "ec2": GROUP_BASE + "grIcon=mxgraph.aws4.group_ec2_instance_contents;strokeColor=#D86613;fillColor=#FFFFFF;fontColor=#D86613;",
    "ec2_opt": GROUP_BASE + "grIcon=mxgraph.aws4.group_ec2_instance_contents;strokeColor=#D86613;fillColor=#FFFFFF;fontColor=#D86613;dashed=1;",
    "corp": GROUP_BASE + "grIcon=mxgraph.aws4.group_corporate_data_center;strokeColor=#7D8998;fillColor=none;fontColor=#5A6C86;",
}
AZ = "fillColor=none;strokeColor=#147EBA;dashed=1;verticalAlign=top;fontStyle=1;fontColor=#147EBA;html=1;whiteSpace=wrap;fontSize=13;align=center;" + FONT

ICON_BASE = (
    "sketch=0;outlineConnect=0;fontColor=#232F3E;gradientColor=none;strokeColor=#ffffff;dashed=0;"
    "verticalLabelPosition=bottom;verticalAlign=top;align=center;html=1;fontSize=11;fontStyle=0;aspect=fixed;" + FONT
)


def res_icon(res, color):
    return ICON_BASE + f"fillColor={color};shape=mxgraph.aws4.resourceIcon;resIcon=mxgraph.aws4.{res};"


def shape_icon(shape, color):
    return ICON_BASE.replace("strokeColor=#ffffff;", "strokeColor=none;") + f"fillColor={color};shape=mxgraph.aws4.{shape};"


ICONS = {
    "users": shape_icon("users", "#232F3E"),
    "user": shape_icon("user", "#232F3E"),
    "igw": shape_icon("internet_gateway", "#8C4FFF"),
    "nat": shape_icon("nat_gateway", "#8C4FFF"),
    "alb": shape_icon("application_load_balancer", "#8C4FFF"),
    "eip": shape_icon("elastic_ip_address", "#ED7100"),
    "rds": res_icon("rds", "#C925D1"),
    "efs": res_icon("elastic_file_system", "#7AA116"),
    "efs_mt": shape_icon("file_system", "#7AA116"),
    "cw": res_icon("cloudwatch_2", "#E7157B"),
    "iam": res_icon("identity_and_access_management", "#DD344C"),
    "r53": res_icon("route_53", "#8C4FFF"),
    "acm": res_icon("certificate_manager_3", "#DD344C"),
    "ssm": res_icon("systems_manager", "#E7157B"),
    "kms": res_icon("key_management_service", "#DD344C"),
}

BOX = (
    "rounded=1;whiteSpace=wrap;html=1;arcSize=10;fontSize=11;align=center;verticalAlign=middle;spacing=4;" + FONT
)
BOX_STYLES = {
    "app": BOX + "fillColor=#FFF4E5;strokeColor=#D86613;fontColor=#232F3E;",
    "mon": BOX + "fillColor=#FDECF4;strokeColor=#E7157B;fontColor=#232F3E;",
    "ai": BOX + "fillColor=#EAF1FB;strokeColor=#147EBA;fontColor=#232F3E;",
    "sec": BOX + "fillColor=#FDEBEC;strokeColor=#DD344C;fontColor=#232F3E;",
    "ext": BOX + "fillColor=#F4F4F4;strokeColor=#5A6C86;fontColor=#232F3E;",
    "legend": BOX + "fillColor=#FFFFFF;strokeColor=#5A6C86;fontColor=#232F3E;align=left;verticalAlign=top;spacing=10;",
    "note": "text;html=1;whiteSpace=wrap;align=left;verticalAlign=top;fontSize=11;fontColor=#5A6C86;" + FONT,
    "title": "text;html=1;whiteSpace=wrap;align=left;verticalAlign=top;fontSize=22;fontStyle=1;fontColor=#232F3E;" + FONT,
    "layer": "rounded=0;whiteSpace=wrap;html=1;fillColor=#F8F9FA;strokeColor=#AAB7B8;verticalAlign=top;align=left;spacingLeft=10;spacingTop=4;fontSize=14;fontStyle=1;fontColor=#232F3E;" + FONT,
    "layer_side": "rounded=1;whiteSpace=wrap;html=1;fillColor=#EAF7EA;strokeColor=#248814;verticalAlign=top;align=center;spacingTop=6;fontSize=13;fontStyle=1;fontColor=#1D6B17;" + FONT,
    "store": "shape=cylinder3;whiteSpace=wrap;html=1;boundedLbl=1;backgroundOutline=1;size=8;fontSize=11;fillColor=#FFFFFF;strokeColor=#5A6C86;fontColor=#232F3E;" + FONT,
    "badge": "ellipse;whiteSpace=wrap;html=1;aspect=fixed;fillColor=#232F3E;strokeColor=#FFFFFF;strokeWidth=2;fontColor=#FFFFFF;fontSize=11;fontStyle=1;spacing=0;" + FONT,
}
EDGE = (
    "edgeStyle=orthogonalEdgeStyle;rounded=1;orthogonalLoop=1;jettySize=auto;html=1;endArrow=block;endFill=1;"
    "strokeWidth=1.5;fontSize=10;labelBackgroundColor=#FFFFFF;" + FONT
)
EDGE_COLORS = {
    "user": "strokeColor=#232F3E;fontColor=#232F3E;",
    "data": "strokeColor=#C925D1;fontColor=#8E1A94;",
    "mon": "strokeColor=#E7157B;fontColor=#A50F57;",
    "ai": "strokeColor=#147EBA;fontColor=#0F5A86;",
    "sec": "strokeColor=#DD344C;fontColor=#A61E30;dashed=1;",
    "cicd": "strokeColor=#7D8998;fontColor=#5A6C86;dashed=1;",
    "plain": "strokeColor=#5A6C86;fontColor=#5A6C86;",
}


class Page:
    def __init__(self, name, width, height):
        self.name, self.width, self.height = name, width, height
        self.cells = []
        self.n = 0

    def _id(self):
        self.n += 1
        return f"c{self.n}"

    def vertex(self, label, x, y, w, h, style):
        cid = self._id()
        self.cells.append(
            f'<mxCell id="{cid}" value="{escape(label, {chr(34): "&quot;"})}" style="{style}" vertex="1" parent="1">'
            f'<mxGeometry x="{x}" y="{y}" width="{w}" height="{h}" as="geometry"/></mxCell>'
        )
        return cid

    def group(self, label, x, y, w, h, kind):
        return self.vertex(label, x, y, w, h, GROUPS[kind] if kind in GROUPS else AZ)

    def icon(self, label, x, y, kind, size=48):
        return self.vertex(label, x, y, size, size, ICONS[kind])

    def box(self, label, x, y, w, h, kind):
        return self.vertex(label, x, y, w, h, BOX_STYLES[kind])

    def edge(self, src, dst, label="", kind="plain", extra="", points=None):
        cid = self._id()
        pts = ""
        if points:
            pts = '<Array as="points">' + "".join(f'<mxPoint x="{px}" y="{py}"/>' for px, py in points) + "</Array>"
        self.cells.append(
            f'<mxCell id="{cid}" value="{escape(label, {chr(34): "&quot;"})}" style="{EDGE}{EDGE_COLORS[kind]}{extra}" '
            f'edge="1" parent="1" source="{src}" target="{dst}"><mxGeometry relative="1" as="geometry">{pts}</mxGeometry></mxCell>'
        )
        return cid

    def xml(self, index):
        body = "".join(self.cells)
        return (
            f'<diagram id="page{index}" name="{escape(self.name)}"><mxGraphModel dx="1600" dy="1000" grid="1" gridSize="10" '
            f'guides="1" tooltips="1" connect="1" arrows="1" fold="1" page="1" pageScale="1" pageWidth="{self.width}" '
            f'pageHeight="{self.height}" math="0" shadow="0"><root><mxCell id="0"/><mxCell id="1" parent="0"/>{body}'
            f"</root></mxGraphModel></diagram>"
        )


# ---------------------------------------------------------------------------
# Page 1: AWS deployment architecture
# ---------------------------------------------------------------------------
p = Page("1. AWS deployment", 2320, 1900)
p.vertex(
    "Safe &amp; Verifiable AI Agent Framework for Infrastructure Operations — AWS deployment (staging)",
    20, 10, 1500, 34, BOX_STYLES["title"],
)
p.vertex(
    "Source of truth: terraform/*.tf, ansible/playbooks/*.yml, release/moodle/docker-compose.yml, .github/workflows. "
    "Moodle is the primary evaluation environment; ERPNext (optional, enable_erpnext) checks generalization.",
    20, 46, 1600, 22, BOX_STYLES["note"],
)

# External actors and services
users = p.icon("<b>Students / Teachers</b><br>Moodle users", 100, 120, "users", 56)
ops = p.icon("<b>Operators</b><br>2 approvers, separate<br>Ed25519 keys", 104, 290, "user", 50)
p.group("External services", 20, 470, 240, 900, "corp")
gh = p.box("<b>GitHub</b><br>Repository + Actions<br>CI: lint, tests, replay, image build", 40, 510, 200, 80, "ext")
ghcr = p.box("<b>GHCR</b><br>ghcr.io/&lt;owner&gt;/moodle-ai-agent<br>Moodle image (immutable SHA tags)", 40, 640, 200, 80, "ext")
idp = p.box("<b>Okta / Microsoft Entra ID</b><br>Moodle SSO (OIDC)", 40, 770, 200, 70, "ext")
gem = p.box("<b>Google Gemini API</b><br>bounded, optional:<br>evidence-cited diagnosis,<br>admin-feedback review", 40, 890, 200, 90, "ext")
tg = p.box("<b>Telegram Bot API</b><br>incident report to operators,<br>/feedback replies", 40, 1030, 200, 80, "ext")
p.vertex(
    "No secrets or raw logs are sent to Gemini: only decoded, sanitized samples and runbook chunks. "
    "The model never plans, executes or verifies.",
    40, 1130, 200, 100, BOX_STYLES["note"],
)

# AWS cloud / region / VPC
p.group("AWS Cloud", 290, 80, 2010, 1560, "cloud")
p.group("Region ap-southeast-1 (Singapore)", 310, 115, 1975, 1510, "region")
p.group("VPC 10.10.0.0/16", 340, 170, 1560, 1440, "vpc")
igw = p.icon("Internet<br>gateway", 1096, 146, "igw")
p.vertex("Availability Zone A", 360, 215, 745, 1380, AZ)
p.vertex("Availability Zone B", 1135, 215, 745, 1380, AZ)

# Public subnets
p.group("Public subnet A 10.10.1.0/24", 380, 245, 705, 160, "public")
p.group("Public subnet B 10.10.2.0/24", 1155, 245, 705, 160, "public")
nat_a = p.icon("NAT gateway A<br>(EIP)", 440, 290, "nat")
nat_b = p.icon("NAT gateway B<br>(EIP)", 1780, 290, "nat")
alb = p.icon(
    "<b>Application Load Balancer</b><br>HTTP :80 / HTTPS :443 (ACM, optional)<br>target group :8080, health /healthz.php",
    1094, 285, "alb", 56,
)

# App subnets
p.group("Private app subnet A 10.10.11.0/24", 380, 430, 705, 420, "private")
p.group("Private app subnet B 10.10.12.0/24", 1155, 430, 705, 420, "private")


def moodle_node(x, name):
    p.group(f"EC2 {name} (t3.small, Amazon Linux 2023) — Docker Compose", x, 465, 470, 365, "ec2")
    web = p.box("<b>moodle-web</b><br>Apache + PHP :8080<br>/var/www/html/public<br>healthz.php, synthetic-transaction.php", x + 20, 505, 190, 80, "app")
    cron = p.box("<b>moodle-cron</b><br>scheduled + ad-hoc tasks", x + 260, 505, 190, 80, "app")
    exp = p.box("<b>node-exporter :9100</b><br><b>+ cAdvisor</b><br>node probes, EFS mount metric", x + 20, 605, 190, 80, "mon")
    exe = p.box("<b>Safe executor</b><br>SSH forced command<br>(allowlisted actions only)", x + 260, 605, 190, 80, "sec")
    mnt = p.box("/var/www/moodledata → EFS (TLS)<br>DB password from secret file", x + 20, 705, 190, 55, "app")
    p.vertex("Image pulled from GHCR (immutable SHA tag); never built on EC2.", x + 260, 705, 190, 50, BOX_STYLES["note"])
    return web, exp, exe, mnt


web_a, exp_a, exe_a, mnt_a = moodle_node(400, "moodle-app-a")
web_b, exp_b, exe_b, mnt_b = moodle_node(1170, "moodle-app-b")
p.group("EC2 ERPNext (optional, enable_erpnext)", 1655, 465, 190, 365, "ec2_opt")
erp = p.box("<b>ERPNext stack</b><br>nginx, app,<br>MariaDB, Redis queue<br>(Docker Compose)", 1670, 505, 160, 110, "app")
p.box("container metrics<br>node-exporter", 1670, 635, 160, 60, "mon")
p.vertex("Second platform for generalization; no business logic dependency.", 1670, 710, 160, 60, BOX_STYLES["note"])

# Data subnets
p.group("Private data subnet A 10.10.21.0/24", 380, 875, 705, 200, "private")
p.group("Private data subnet B 10.10.22.0/24", 1155, 875, 705, 200, "private")
rds = p.icon("<b>Amazon RDS for PostgreSQL</b><br>db.t4g.micro, single-AZ, TLS<br>parameter group, logs → CloudWatch", 470, 935, "rds", 56)
mt_a = p.icon("EFS mount target A<br>NFS :2049", 860, 940, "efs_mt")
mt_b = p.icon("EFS mount target B<br>NFS :2049", 1640, 940, "efs_mt")
p.vertex("DB subnet group spans data A + B<br>(Multi-AZ standby disabled for staging cost)", 1190, 935, 330, 50, BOX_STYLES["note"])

# Management subnet + monitor host
p.group("Management subnet 10.10.31.0/24 (AZ A)", 380, 1100, 705, 480, "public")
eip = p.icon("Elastic IP", 1020, 1112, "eip", 36)
p.group("EC2 monitor-ai-01 — control plane (IAM role monitor_audit)", 395, 1150, 675, 415, "ec2")
p.vertex("<b>Observability</b>", 410, 1185, 200, 20, BOX_STYLES["note"])
prom = p.box("<b>Prometheus</b><br>Moodle alert rules", 410, 1210, 150, 60, "mon")
am = p.box("<b>Alertmanager</b><br>routes → agent webhook", 575, 1210, 150, 60, "mon")
bb = p.box("<b>Blackbox exporter</b><br>ALB probe, RDS TCP probe", 410, 1285, 150, 60, "mon")
graf = p.box("<b>Grafana</b><br>infrastructure dashboard", 575, 1285, 150, 60, "mon")
syn = p.box("Synthetic transaction runner<br>+ RDS SG ingress audit (read-only)", 410, 1360, 315, 50, "mon")
p.vertex("<b>AI Agent framework</b>", 745, 1185, 200, 20, BOX_STYLES["note"])
agent = p.box("<b>moodle-ai-agent</b> (FastAPI :8000)<br>/webhook, remediation prepare/execute,<br>verifier attestation endpoints", 745, 1210, 310, 60, "ai")
worker = p.box("<b>moodle-agent-worker</b> (Celery)<br>Observer → Diagnosis → Planner", 745, 1285, 200, 60, "ai")
redis = p.box("<b>moodle-agent-redis</b><br>queue + cooldown", 955, 1285, 100, 60, "ai")
stores = p.vertex("Evidence store (SQLite, hash-chained ledger) · ChromaDB runbooks · safe-action audit", 745, 1360, 310, 50, BOX_STYLES["store"])
api = p.box("<b>Safe executor API</b><br>Safety Gate + 2 approvals", 695, 1425, 160, 60, "sec")
ver = p.box("<b>Independent Verifier</b><br>separate identity,<br>read-only probes", 895, 1425, 160, 60, "sec")
runner = p.box("<b>GitHub self-hosted runner</b><br>(moodle-staging)<br>rolling deploy B → A over SSH", 410, 1425, 260, 60, "ext")
p.vertex("Agent dispatch defaults off; alerts never execute actions.", 410, 1500, 640, 20, BOX_STYLES["note"])

# Regional services (outside VPC)
p.vertex("<b>Regional services</b>", 1930, 185, 300, 20, BOX_STYLES["note"])
efs = p.icon("<b>Amazon EFS</b> moodledata<br>Regional, encrypted,<br>access point + backup policy", 2040, 230, "efs")
cw = p.icon("<b>CloudWatch Logs</b><br>RDS PostgreSQL log group", 2040, 400, "cw")
iam = p.icon("<b>IAM</b> role monitor_audit<br>ec2:DescribeSecurityGroupRules only;<br>operator roles require MFA", 2040, 560, "iam")
r53 = p.icon("<b>Route 53 health check</b><br>external TCP to RDS endpoint<br>(must stay unreachable)", 2040, 740, "r53")
acm = p.icon("<b>ACM</b> certificate<br>(HTTPS listener, optional)", 2040, 910, "acm")
ssm = p.icon("<b>SSM Parameter Store</b><br>AL2023 AMI ID", 2040, 1060, "ssm")
p.box("<b>Security groups</b><br>alb_sg: 80/443 from Internet<br>moodle_sg: 8080 from ALB only<br>rds_sg: 5432 from moodle/monitor<br>efs_sg: 2049 from moodle<br>monitor_sg: SSH from operator CIDR", 1930, 1210, 330, 130, "sec")

# Edges: only short, local flows; long flows are numbered badges (see legend).
def badge(n, x, y):
    return p.vertex(n, x, y, 24, 24, BOX_STYLES["badge"])


p.edge(users, igw, "① HTTPS", "user", points=[(1120, 148)])
p.edge(igw, alb, "", "user")
p.edge(alb, web_a, "② :8080", "user", "exitX=0;exitY=0.5;entryX=0.5;entryY=0;", points=[(515, 313)])
p.edge(alb, web_b, "② :8080", "user", "exitX=1;exitY=0.5;entryX=0.5;entryY=0;", points=[(1265, 313)])
p.edge(web_a, rds, "③ PostgreSQL TLS :5432<br>(both nodes)", "data", "exitX=1;exitY=0.5;entryX=1;entryY=0.5;", points=[(635, 545), (635, 963)])
p.edge(mnt_a, mt_a, "NFS :2049", "data", "exitX=0.5;exitY=1;entryX=0.5;entryY=0;", points=[(515, 905), (884, 905)])
p.edge(mnt_b, mt_b, "NFS :2049", "data", "exitX=0.5;exitY=1;entryX=0.5;entryY=0;", points=[(1285, 905), (1664, 905)])
p.edge(mt_b, efs, "Regional file system", "data", "dashed=1;exitX=1;exitY=0.5;entryX=0;entryY=0.5;", points=[(1905, 964), (1905, 254)])
p.edge(exp_a, prom, "④ scrape (both nodes)", "mon", "exitX=0;exitY=0.5;entryX=0;entryY=0.5;", points=[(390, 645), (390, 1240)])
p.edge(prom, am, "alerts", "mon")
p.edge(am, agent, "⑤ webhook", "mon", "exitX=1;exitY=0.5;entryX=0;entryY=0.5;")
p.edge(agent, worker, "Celery task", "ai", "exitX=0.25;exitY=1;entryX=0.5;entryY=0;")
p.edge(api, ver, "receipt", "sec", "exitX=1;exitY=0.5;entryX=0;entryY=0.5;")
p.edge(gh, ghcr, "build + push images", "cicd")
p.edge(nat_a, ghcr, "egress via NAT: GHCR pull, OIDC to IdP", "cicd", "exitX=0;exitY=0.5;entryX=1;entryY=0.5;", points=[(300, 314), (300, 680)])

# Badges for flows that cross the diagram
for n, x, y in [
    ("6", 1032, 1200), ("6", 228, 880),
    ("7", 1000, 1200), ("7", 228, 1020),
    ("8", 150, 286), ("8", 683, 1413),
    ("9", 843, 1413), ("9", 640, 597), ("9", 1410, 597),
    ("10", 883, 1413), ("10", 968, 1200),
    ("11", 228, 500), ("11", 658, 1413), ("11", 640, 497), ("11", 1410, 497),
    ("12", 713, 1350), ("12", 2085, 553),
]:
    badge(n, x, y)

# Legend
p.box(
    "<b>Flow legend</b> (numbered edge labels and black badges)<br>"
    "① Users reach Moodle through the IGW and ALB (HTTPS with ACM when configured).<br>"
    "② ALB forwards to both app nodes on :8080; one healthy target is kept during rolling deploys.<br>"
    "③ Moodle uses RDS PostgreSQL (TLS) and shared moodledata on EFS through per-AZ mount targets.<br>"
    "④ Prometheus scrapes node-exporter, cAdvisor and node probes; Blackbox probes the ALB and RDS path.<br>"
    "⑤ Alertmanager posts to the agent webhook; the Celery worker runs Observer → Diagnosis → Planner (read-only).<br>"
    "⑥ Optional bounded Gemini call for evidence-cited diagnosis; a deterministic citation validator must accept it.<br>"
    "⑦ Operator report and feedback over Telegram.<br>"
    "⑧ Remediation needs two separate Ed25519 approvals bound to the plan hash, scope and expiry.<br>"
    "⑨ The Safety Gate re-checks, then the executor runs one allowlisted action via an SSH forced command.<br>"
    "⑩ The executor receipt goes to the Independent Verifier; only its signed attestation can mark the incident RESOLVED.<br>"
    "⑪ CI builds immutable images to GHCR; the self-hosted runner rolls node B then node A over SSH.<br>"
    "⑫ The RDS ingress audit uses the monitor IAM role (ec2:DescribeSecurityGroupRules only).<br>"
    "⑥ ⑦ leave through the monitor Elastic IP; app-node egress (GHCR pull, OIDC) uses the NAT gateways.",
    290, 1660, 1100, 240, "legend",
)
p.box(
    "<b>Line colours</b><br>"
    "<font color='#232F3E'>■</font> user traffic &nbsp; <font color='#C925D1'>■</font> data &nbsp; "
    "<font color='#E7157B'>■</font> monitoring &nbsp; <font color='#147EBA'>■</font> AI agent<br>"
    "<font color='#DD344C'>■</font> safety / approval (dashed) &nbsp; <font color='#7D8998'>■</font> CI/CD and egress (dashed)",
    1410, 1660, 470, 90, "legend",
)

# ---------------------------------------------------------------------------
# Page 2: AI agent logical architecture (six layers)
# ---------------------------------------------------------------------------
q = Page("2. AI agent framework", 2000, 1500)
q.vertex("AI agent framework — six-layer logical architecture and incident lifecycle", 20, 10, 1500, 34, BOX_STYLES["title"])
q.vertex(
    "Layers follow the thesis outline. Shared IDs: incident_id, evidence_id, action_id. "
    "Every stage writes a hash-chained StageAuditEvent (docs/RUNTIME_AGENT_ROLES.md).",
    20, 46, 1600, 22, BOX_STYLES["note"],
)
LX, LW = 20, 1560
layers = [
    ("1  Observation", 80, 150),
    ("2  Context &amp; Knowledge", 245, 165),
    ("3  Agents", 425, 185),
    ("4  Policy &amp; Security", 625, 165),
    ("5  Execution &amp; Tools", 805, 150),
    ("6  Infrastructure Adapters", 970, 150),
]
for name, y, h in layers:
    q.vertex(name, LX, y, LW, h, BOX_STYLES["layer"])

# Layer 1
s_prom = q.box("<b>Prometheus + Alertmanager</b><br>Moodle alert rules", 60, 120, 220, 70, "mon")
s_probe = q.box("<b>Probes</b><br>node probes, Blackbox,<br>synthetic transaction, SG audit", 300, 120, 220, 70, "mon")
s_hook = q.box("<b>/webhook</b> (FastAPI)<br>validate, staging-only", 600, 120, 200, 70, "ai")
s_queue = q.box("<b>Celery + Redis</b><br>cooldown / dedup by fingerprint", 830, 120, 230, 70, "ai")
q.edge(s_probe, s_prom, "metrics", "mon")
q.edge(s_prom, s_hook, "alert", "mon", "exitX=0.5;exitY=0;entryX=0.5;entryY=0;", points=[(170, 100), (700, 100)])
q.edge(s_hook, s_queue, "enqueue", "ai")

# Layer 2
k_col = q.box("<b>Evidence collectors</b><br>approved PromQL per alert,<br>alert context, config + topology snapshot", 60, 290, 250, 80, "ai")
k_store = q.vertex("<b>Evidence store</b><br>SQLite, content hash,<br>append-only + checkpoints", 340, 285, 200, 90, BOX_STYLES["store"])
k_graph = q.box("<b>Resource model + dependency graph</b><br>moodle_resource_inventory.json<br>impact: confirmed / potential", 570, 290, 250, 80, "ai")
k_rag = q.box("<b>RAG runbooks</b><br>ChromaDB or static retriever<br>chunk_id + SHA-256 provenance", 850, 290, 230, 80, "ai")
k_links = q.box("<b>knowledge_links</b><br>evidence ↔ runbook chunk ↔ conclusion", 1110, 290, 230, 80, "ai")

# Layer 3
a_obs = q.box("<b>ObserverAgent</b><br>ObservationReport", 60, 480, 180, 70, "ai")
a_diag = q.box("<b>DiagnosisAgent</b><br>mode: rules | llm | hybrid<br>closed cause vocabulary", 290, 470, 220, 90, "ai")
a_llm = q.box("<b>Gemini</b> (bounded)<br>temp 0, no tools,<br>timeout, ≤1024 tokens", 540, 450, 170, 70, "ext")
a_val = q.box("<b>Citation validator</b><br>17 violation codes;<br>fails closed", 540, 535, 170, 65, "sec")
a_plan = q.box("<b>PlannerAgent</b><br>catalog only: read-only probe<br>or exact-fault candidate", 760, 470, 230, 90, "ai")
a_gate = q.box("<b>gate_request</b><br>awaiting two approvals", 1040, 480, 180, 70, "sec")
a_ledger = q.vertex("Stage ledger (hash chain)<br>observe → diagnose → plan → gate_request", 1250, 470, 300, 90, BOX_STYLES["store"])

# Layer 4
g_cat = q.box("<b>Action catalog + policy engine</b><br>allowlist, hard-deny patterns,<br>environment, role, target scope", 60, 665, 260, 90, "sec")
g_dry = q.box("<b>Dry-run</b><br>read state, expected diff;<br>no mutation", 350, 665, 190, 90, "sec")
g_hum = q.box("<b>Human gate</b><br>2 Ed25519 approvals bound to<br>plan hash, scope, expiry", 570, 665, 240, 90, "sec")
g_re = q.box("<b>Re-check before execute</b><br>hash, permission, freshness,<br>expiry, kill switch → fail closed", 840, 665, 250, 90, "sec")

# Layer 5
e_agent = q.box("<b>ExecutionAgent</b><br>prepare / execute handoff", 60, 845, 220, 75, "ai")
e_api = q.box("<b>Safe executor API</b><br>(monitor host)", 310, 845, 190, 75, "sec")
e_ssh = q.box("<b>SSH forced command</b><br>one allowlisted action,<br>timeout, idempotency key", 530, 845, 220, 75, "sec")
e_rb = q.box("<b>Conditional rollback</b><br>reviewed baseline only", 780, 845, 200, 75, "sec")
e_audit = q.vertex("Safe-action audit<br>(hash-chained receipts)", 1010, 840, 200, 85, BOX_STYLES["store"])

# Layer 6
i_moodle = q.box("<b>Moodle adapter</b><br>7 thesis scenarios", 60, 1010, 180, 70, "app")
i_erp = q.box("<b>ERPNext adapter</b><br>3 scenarios (generalization)", 260, 1010, 200, 70, "app")
i_docker = q.box("<b>Docker Compose</b><br>moodle-web / cron", 480, 1010, 170, 70, "app")
i_ansible = q.box("<b>Ansible</b><br>host config, baseline<br>remediation (comparison)", 670, 1010, 190, 70, "app")
i_aws = q.box("<b>AWS APIs</b><br>EC2 SG rules (read-only)", 880, 1010, 180, 70, "app")
i_prom = q.box("<b>Prometheus / Alertmanager</b><br>HTTP API", 1080, 1010, 200, 70, "app")

# Independent verifier (side column)
v_side = q.vertex("Independent Verifier<br>(separate identity)", 1610, 80, 360, 720, BOX_STYLES["layer_side"])
v_probe = q.box("<b>Fresh probes</b><br>allowed, forbidden and related<br>flows from the communication<br>contract + health check", 1640, 200, 300, 100, "sec")
v_stab = q.box("<b>Stability window</b><br>N consecutive passing rounds", 1640, 340, 300, 70, "sec")
v_att = q.box("<b>Signed attestation</b> (Ed25519)", 1640, 450, 300, 60, "sec")
v_agent = q.box("<b>VerificationAgent</b><br>only path to RESOLVED;<br>health-only pass is not enough", 1640, 550, 300, 90, "ai")
v_res = q.box("<b>RESOLVED</b> or FAILED<br>+ conditional rollback", 1640, 690, 300, 70, "ai")

# Evaluation lane
q.vertex("Evaluation (offline + live campaign)", LX, 1140, 1950, 180, BOX_STYLES["layer"])
q.box("<b>Fault injection / reset</b><br>moodle-fault-*.sh, erpnext-fault-*.sh", 60, 1180, 230, 70, "ext")
q.box("<b>Three methods</b><br>manual · Ansible · AI agent<br>same initial state and criteria", 320, 1180, 250, 70, "ext")
q.box("<b>RQ1 ablations</b><br>no Safety Gate; LLM planner<br>with / without gate", 600, 1180, 230, 70, "ext")
q.box("<b>RQ2 ablation</b><br>health-only vs<br>communication contract", 860, 1180, 210, 70, "ext")
q.box("<b>AI diagnosis evaluation</b><br>rules vs LLM vs LLM+RAG: accuracy,<br>stability, robustness, cost", 1100, 1180, 270, 70, "ext")
q.box("<b>Scorer</b><br>RCA, recovery, false recovery,<br>blocked actions, LLM calls/tokens, AWS cost", 1400, 1180, 290, 70, "ext")
q.vertex("Ground-truth fixtures are post-decision scoring data, never decision input.", 60, 1270, 900, 20, BOX_STYLES["note"])

# Page 2 edges
q.edge(s_queue, a_obs, "", "ai", "exitX=0.5;exitY=1;entryX=0;entryY=0.5;", points=[(945, 232), (40, 232), (40, 515)])
q.edge(a_obs, k_col, "collect", "ai", "exitX=0.25;exitY=0;entryX=0.25;entryY=1;")
q.edge(k_col, k_store, "append", "ai")
q.edge(a_obs, a_diag, "", "ai")
q.edge(k_rag, a_diag, "runbook chunks", "ai", "exitX=0.5;exitY=1;entryX=0.75;entryY=0;", points=[(965, 410), (455, 410)])
q.edge(a_diag, a_llm, "prompt", "ai", "exitX=1;exitY=0.25;entryX=0;entryY=0.5;")
q.edge(a_llm, a_val, "hypotheses", "ai")
q.edge(a_val, a_diag, "accepted only", "sec", "exitX=0;exitY=0.5;entryX=1;entryY=0.8;")
q.edge(a_diag, a_plan, "DiagnosisReport", "ai", "exitX=0.5;exitY=1;entryX=0.5;entryY=1;", points=[(400, 603), (875, 603)])
q.edge(k_graph, a_plan, "impact", "ai", "exitX=0.75;exitY=1;entryX=0.25;entryY=0;")
q.edge(a_diag, k_links, "decision record", "ai", "exitX=0.5;exitY=0;entryX=0.5;entryY=1;", points=[(400, 395), (1225, 395)])
q.edge(a_plan, a_gate, "PlanProposal", "ai")
q.edge(a_gate, a_ledger, "", "plain", "dashed=1;")
q.edge(a_gate, g_cat, "", "sec", "exitX=0.5;exitY=1;entryX=0.5;entryY=0;", points=[(1130, 610), (190, 610)])
q.edge(g_cat, g_dry, "", "sec")
q.edge(g_dry, g_hum, "", "sec")
q.edge(g_hum, g_re, "", "sec")
q.edge(g_re, e_agent, "", "sec", "exitX=0.5;exitY=1;entryX=0.5;entryY=0;", points=[(965, 785), (170, 785)])
q.edge(e_agent, e_api, "", "sec")
q.edge(e_api, e_ssh, "", "sec")
q.edge(e_ssh, e_audit, "receipt", "sec", "exitX=0.5;exitY=1;entryX=0.5;entryY=1;", points=[(640, 945), (1110, 945)])
q.edge(e_ssh, i_docker, "", "sec", "exitX=0.5;exitY=1;entryX=0.5;entryY=0;")
q.edge(e_audit, v_probe, "awaiting verification", "sec", "exitX=1;exitY=0.5;entryX=0;entryY=0.5;", points=[(1590, 882), (1590, 250)])
q.edge(v_probe, v_stab, "", "sec")
q.edge(v_stab, v_att, "", "sec")
q.edge(v_att, v_agent, "", "sec")
q.edge(v_agent, v_res, "", "ai")
q.edge(v_res, e_rb, "fail → rollback if safe", "sec", "exitX=0.5;exitY=1;entryX=0.5;entryY=0;", points=[(1790, 790), (880, 790)])

doc = (
    '<mxfile host="app.diagrams.net" agent="thesis-generator" version="24.7.17">'
    + p.xml(1)
    + q.xml(2)
    + "</mxfile>"
)
with open(OUT, "w", encoding="utf-8") as fh:
    fh.write(doc)
print("wrote", OUT, len(p.cells), len(q.cells))
