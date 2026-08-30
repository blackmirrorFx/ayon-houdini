cmake_minimum_required(VERSION 3.17)

project(BMFX_APKG_Block_Begin)

list(APPEND CMAKE_PREFIX_PATH "$ENV{HFS}/toolkit/cmake")
find_package(Houdini REQUIRED)

add_library(BMFX_APKG_Block_Begin SHARED
    LOP_BMFXAPKGBlockBegin.C
    LOP_BMFXAPKGBlockBegin.h
)

target_link_libraries(
    BMFX_APKG_Block_Begin
    Houdini
    HoudiniThirdParty
)

if(NOT CMAKE_LIBRARY_OUTPUT_DIRECTORY)
    set(CMAKE_LIBRARY_OUTPUT_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}/../../dso")
endif()
houdini_configure_target(
    BMFX_APKG_Block_Begin
    INSTDIR "${CMAKE_LIBRARY_OUTPUT_DIRECTORY}"
)
