from enum import Enum


class IntentType(str, Enum):
    PRICE = "price"
    CHANNELS = "channels"
    SERVICE_DESCRIPTION = "service_description"
    INDUSTRIES = "industries"
    USE_CASES = "use_cases"
    INTEREST_SIGNAL = "interest_signal"
    REJECTION = "rejection"
    FRUSTRATED = "frustrated"
    AGGRESSIVE_OBJECTION = "aggressive_objection"
    HESITATION = "hesitation"
    BUYING_SIGNAL = "buying_signal"
    START_REQUIREMENTS = "start_requirements"
    CONSULTATION_INTEREST = "consultation_interest"
    BOOKING_REQUEST = "booking_request"
    GENERAL_QUESTION = "general_question"


class BookingState(str, Enum):
    NONE = "NONE"
    WAITING_FOR_TIME = "WAITING_FOR_TIME"
    WAITING_FOR_CONTACT = "WAITING_FOR_CONTACT"
    CONFIRMATION = "CONFIRMATION"
    CONFIRMED = "CONFIRMED"
