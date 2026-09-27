plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.android)
    alias(libs.plugins.kotlin.compose)
    alias(libs.plugins.kotlin.serialization)
    alias(libs.plugins.ksp)
    alias(libs.plugins.hilt)
}

android {
    namespace = "in.aapatmitra"
    compileSdk = 35

    defaultConfig {
        applicationId = "in.aapatmitra"
        minSdk = 24            // Android 7.0, 2 GB phones (TRD §3.1)
        targetSdk = 35
        versionCode = 1
        versionName = "1.0.0"
        // Emulator → host machine. Real devices: set -PapiBaseUrl=https://api.<domain>/api/v1/
        buildConfigField("String", "API_BASE", "\"${project.findProperty("apiBaseUrl") ?: "http://10.0.2.2:8080/api/v1/"}\"")
        buildConfigField("String", "DEFAULT_DISTRICT", "\"0915\"")
        // Fallback channel numbers until the first sync delivers `channel_numbers` (seeded demo district 0915)
        buildConfigField("String", "SMS_NUMBER", "\"${project.findProperty("smsNumber") ?: "+911204567890"}\"")
        buildConfigField("String", "IVR_NUMBER", "\"${project.findProperty("ivrNumber") ?: "+911204567891"}\"")
        buildConfigField("String", "HELPLINE", "\"108\"")
        ksp { arg("room.schemaLocation", "$projectDir/schemas") }   // exportSchema → Room migration tests (database.md §14.9)
    }

    buildTypes {
        debug {
            // cleartext to the local stack only (network_security_config debug-overrides)
            manifestPlaceholders["networkConfig"] = "@xml/network_security_config_debug"
        }
        release {
            isMinifyEnabled = true              // R8 full mode (FR-A05, SEC-MOB-11)
            isShrinkResources = true
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
            manifestPlaceholders["networkConfig"] = "@xml/network_security_config"
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
        isCoreLibraryDesugaringEnabled = true
    }
    kotlinOptions { jvmTarget = "17" }
    buildFeatures { compose = true; buildConfig = true }
    packaging { resources.excludes += "/META-INF/{AL2.0,LGPL2.1}" }
}

dependencies {
    coreLibraryDesugaring("com.android.tools:desugar_jdk_libs:2.1.3")   // java.time on API 24
    implementation(libs.androidx.core)
    implementation(libs.androidx.activity.compose)
    implementation(platform(libs.compose.bom))
    implementation(libs.compose.ui)
    implementation(libs.compose.material3)
    implementation(libs.compose.icons)
    implementation(libs.compose.tooling.preview)
    implementation(libs.lifecycle.viewmodel.compose)
    implementation(libs.lifecycle.runtime.compose)
    implementation(libs.navigation.compose)
    implementation(libs.hilt.android)
    ksp(libs.hilt.compiler)
    implementation(libs.hilt.navigation.compose)
    implementation(libs.hilt.work)
    ksp(libs.hilt.work.compiler)
    implementation(libs.room.runtime)
    implementation(libs.room.ktx)
    ksp(libs.room.compiler)
    implementation(libs.work.runtime)
    implementation(libs.retrofit)
    implementation(libs.retrofit.serialization)
    implementation(libs.okhttp)
    implementation(libs.serialization.json)
    implementation(libs.coroutines.android)
    implementation(libs.datastore.preferences)
    implementation(libs.sqlcipher)
    implementation(libs.sqlite)
    implementation(libs.zxing.core)
    implementation(libs.zxing.embedded)
    testImplementation(libs.junit)
}
