"""Import every model so Base.metadata knows all tables."""
from app.models.system import (  # noqa: F401
    AbuseEvent, AIMessage, AuditLog, GovScheme, IpBan, JobRun, KnowledgeChunk, LoadTestRun, MinimumWage, Notification,
    OutboundMessage, PendingAction, PlatformSetting, SchemeSnapshot, SchemeVisit, TranslationCache, Upload, UserSanction,
)
from app.models.user import AccountDeletion, IdentityDocument, OtpCode, UnbanRequest, User  # noqa: F401
from app.models.farm import (  # noqa: F401
    Branch, BranchCrop, BranchDay, CalamityEvent, Farm, FarmMember, FarmProposal, GstinVerification, Halt,
    Holiday, InsurancePolicy, ProposalVote, TermsAcceptance, TermsReview, WeatherReport,
)
from app.models.jobs import ExternalJob, Invitation, JobApplication, JobPosting  # noqa: F401
from app.models.welfare import Grievance, OwnerWarning, SafetyCase, WellbeingSurvey  # noqa: F401
from app.models.payroll import (  # noqa: F401
    DueAdjustment, LedgerEntry, PersonalLedgerEntry, SalaryAccrual, SalaryPayment,
)
from app.models.shop import (  # noqa: F401
    InventoryMovement, Order, OrderEvent, OrderItem, Product, ProductQuestion, Review, Shop,
)
