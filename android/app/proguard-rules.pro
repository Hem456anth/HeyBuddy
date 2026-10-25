# HeyBuddy release rules (R8 full mode).
# kotlinx.serialization: keep generated serializers of @Serializable classes.
-keepclassmembers class com.heybuddy.core.** {
    *** Companion;
}
-keepclasseswithmembers class com.heybuddy.core.** {
    kotlinx.serialization.KSerializer serializer(...);
}
-keep,includedescriptorclasses class com.heybuddy.core.**$$serializer { *; }
# Services/receivers are referenced from the manifest (kept by AAPT rules).
-dontwarn org.conscrypt.**
-dontwarn org.bouncycastle.**
-dontwarn org.openjsse.**
